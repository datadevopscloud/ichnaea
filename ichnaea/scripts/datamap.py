#!/usr/bin/env python3
"""
Generate datamap image tiles and upload them to Amazon S3.

The process is:

1. Export data from datamap tables to CSV.
   The data is exported as pairs of latitude and longitude,
   converted into 0 to 6 pairs randomly around that point.
2. Convert the data into quadtree structures.
   This structure is more efficient for finding the points that
   apply to a tile.
3. Merge the per-table quadtrees into a single file
4. Generate tiles for each zoom level.
   More tiles, covering a smaller distance, are created at each
   higher zoom level.
5. Update the S3 bucket with the new tiles.
   The MD5 checksum is used to determine if a tile is unchanged.
   New tiles are uploaded, and orphaned tiles are deleted.

The quadtree and tile generators are from:
https://github.com/ericfischer/datamaps

The generated tiles are minimized with pngquant:
https://pngquant.org
"""

import argparse
import glob
import hashlib
import os
import os.path
import shutil
import subprocess
import sys
import uuid
from collections import defaultdict
from json import dumps
from multiprocessing import Pool
from timeit import default_timer

import boto3
import botocore
import structlog
from more_itertools import chunked
from sqlalchemy import text

from geocalc import random_points
from ichnaea import util
from ichnaea.conf import settings
from ichnaea.db import configure_db, db_worker_session
from ichnaea.log import configure_logging, configure_raven
from ichnaea.models.content import DataMap, decode_datamap_grid


LOG = structlog.get_logger("ichnaea.scripts.datamap")
S3_CLIENT = None  # Will be re-initialized in each pool thread


class Timer:
    """Context-based timer."""

    def __enter__(self):
        self.start = default_timer()
        return self

    def __exit__(self, *args):
        self.end = default_timer()
        self.duration_s = round(self.end - self.start, 3)

    @property
    def elapsed(self):
        return default_timer() - self.start


def generate(
    output_dir,
    bucket_name,
    raven_client,
    create=True,
    upload=True,
    concurrency=2,
    max_zoom=11,
):
    """
    Process datamaps tables into tiles and optionally upload them.

    :param output_dir: The base directory for working files and tiles
    :param bucket_name: The name of the S3 bucket for upload
    :param raven_client: A raven client to log exceptions
    :param upload: True (default) if tiles should be uploaded to S3
    :param concurrency: The number of simultanous worker processes
    :param max_zoom: The maximum zoom level to generate
    :return: Details of the process
    :rtype: dict
    """
    result = {}

    # Setup directories
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    csv_dir = os.path.join(output_dir, "csv")
    quadtree_dir = os.path.join(output_dir, "quadtrees")
    shapes_dir = os.path.join(output_dir, "shapes")
    tiles_dir = os.path.abspath(os.path.join(output_dir, "tiles"))

    if create:
        LOG.debug("Generating tiles from datamap tables...")

        # Export datamap table to CSV files
        if not os.path.isdir(csv_dir):
            os.mkdir(csv_dir)

        row_count = None
        with Pool(processes=concurrency) as pool, Timer() as export_timer:
            row_count, csv_count = export_to_csvs(pool, csv_dir)
        result["export_duration_s"] = export_timer.duration_s
        result["row_count"] = row_count
        result["csv_count"] = csv_count
        LOG.debug(
            f"Exported {row_count:,} row{_s(row_count)}"
            f" to {csv_count:,} CSV{_s(csv_count)}"
            f" in {export_timer.duration_s:0.1f} seconds"
        )
        if result["row_count"] == 0:
            LOG.debug("No rows to export, so no tiles to generate.")
            return result

        # Convert CSV files to per-table quadtrees
        if os.path.isdir(quadtree_dir):
            shutil.rmtree(quadtree_dir)
        os.mkdir(quadtree_dir)

        with Pool(processes=concurrency) as pool, Timer() as quadtree_timer:
            quad_result = csv_to_quadtrees(pool, csv_dir, quadtree_dir)
        csv_converted, intermediate, final = quad_result
        result["quadtree_duration_s"] = quadtree_timer.duration_s
        result["csv_converted_count"] = csv_converted
        result["intermediate_quadtree_count"] = intermediate
        result["quadtree_count"] = final
        LOG.debug(
            f"Processed {csv_converted:,} CSV{_s(csv_converted)}"
            f" into {intermediate:,} intermediate quadtree{_s(intermediate)}"
            f" and {final:,} region quadtree{_s(final)}"
            f" in {quadtree_timer.duration_s:0.1f} seconds"
        )

        # Merge quadtrees and make points unique.
        if os.path.isdir(shapes_dir):
            shutil.rmtree(shapes_dir)

        with Timer() as merge_timer:
            merge_quadtrees(quadtree_dir, shapes_dir)
        result["merge_duration_s"] = merge_timer.duration_s
        LOG.debug(f"Merged quadtrees in {merge_timer.duration_s:0.1f} seconds")

        # Render tiles
        with Pool(processes=concurrency) as pool, Timer() as render_timer:
            tile_count = render_tiles(pool, shapes_dir, tiles_dir, max_zoom)
        result["tile_count"] = tile_count
        result["render_duration_s"] = render_timer.duration_s
        LOG.debug(
            f"Rendered {tile_count:,} tile{_s(tile_count)}"
            f" in {render_timer.duration_s:0.1f} seconds"
        )

    if upload:
        LOG.debug(f"Syncing tiles to S3 bucket {bucket_name}...")

        # Determine the sync plan by comparing S3 to the local tiles
        # This function times itself
        plan, unchanged_count = get_sync_plan(bucket_name, tiles_dir)

        # Sync local tiles with S3 bucket
        # Double concurrency since I/O rather than CPU bound
        # Max tasks to free accumulated memory from the S3 clients
        with Pool(
            processes=concurrency * 2, maxtasksperchild=1000
        ) as pool, Timer() as sync_timer:
            sync_counts = sync_tiles(
                pool, plan, bucket_name, tiles_dir, max_zoom, raven_client
            )

        result["sync_duration_s"] = sync_timer.duration_s
        result["tiles_unchanged"] = unchanged_count
        result.update(sync_counts)
        LOG.debug(
            f"Synced tiles to S3 in {sync_timer.duration_s:0.1f} seconds: "
            f"{sync_counts['tile_new']:,} new, "
            f"{sync_counts['tile_changed']:,} changed, "
            f"{sync_counts['tile_deleted']:,} deleted, "
            f"{sync_counts['tile_failed']:,} failed, "
            f"{unchanged_count:,} unchanged"
        )

        upload_status_file(bucket_name, result)

    return result


def _s(count):
    """Add an s, like rows, if the count is not 1."""
    if count == 1:
        return ""
    else:
        return "s"


def export_to_csvs(pool, csv_dir):
    """
    Export from database tables to CSV.

    For small database tables, there will be one CSV created, such as
    "map_ne.csv" for the datamap_ne (northeast) table.

    For large database tables, there will be multiple CSVs created,
    such as "submap_ne_0001.csv".

    :param pool: A multiprocessing pool
    :csv_dir: The directory to write CSV output files
    :return: A tuple of counts (rows, CSVs)
    """
    jobs = []
    result_rows = 0
    result_csvs = 0
    for shard_id, shard in sorted(DataMap.shards().items()):
        # sorting the shards prefers the north which contains more
        # data points than the south
        filename = f"map_{shard_id}.csv"
        jobs.append(
            pool.apply_async(export_to_csv, (filename, csv_dir, shard.__tablename__))
        )

    # Run export jobs to completion
    def on_success(result):
        nonlocal result_rows, result_csvs
        rows, csvs = result
        result_rows += rows
        result_csvs += csvs

    def on_progress(tables_complete, table_percent):
        nonlocal result_rows
        LOG.debug(
            f"  Exported {result_rows:,} row{_s(result_rows)}"
            f" from {tables_complete:,} table{_s(tables_complete)}"
            f" to {result_csvs:,} CSV file{_s(result_csvs)}"
            f" ({table_percent:0.1%})"
        )

    watch_jobs(jobs, on_success=on_success, on_progress=on_progress)
    return result_rows, result_csvs


def watch_jobs(
    jobs,
    on_success=None,
    on_error=None,
    on_progress=None,
    raven_client=None,
    progress_seconds=5.0,
):
    """Watch async jobs as they complete, periodically reporting progress.

    :param on_success: A function to call with the job output, skip if None
    :param on_error: A function to call with the exception, re-raises if None
    :param on_progress: A function to call to report progress, passed jobs complete and percent of total
    :param raven_client: The raven client to capture exceptions (optional)
    :param progress_seconds: How often to call on_progress
    """

    with Timer() as timer:
        last_elapsed = 0.0
        total_jobs = len(jobs)
        jobs_complete = 0
        for job in jobs:
            if timer.elapsed > (last_elapsed + progress_seconds):
                job_percent = jobs_complete / total_jobs
                on_progress(jobs_complete, job_percent)
                last_elapsed = timer.elapsed

            try:
                job_resp = job.get()
                if on_success:
                    on_success(job_resp)
            except KeyboardInterrupt:
                # Skip Raven for Ctrl-C, reraise to halt execution
                raise
            except Exception as e:
                if raven_client:
                    raven_client.captureException()
                if on_error:
                    on_error(e)
                else:
                    raise
            jobs_complete += 1


def csv_to_quadtrees(pool, csvdir, quadtree_dir):
    """
    Convert CSV to quadtrees.

    :param pool: A multiprocessing pool
    :param csvdir: The directory with the input CSV files
    :param quadtree_dir: The directory with the output quadtree files
    :return: A tuple of counts (CSVs processed, intermediate quadtrees, final quads)

    If multiple CSVs were generated for a datamap table, then per-CSV intermediate
    quadtrees will be created in a subfolder, and then merged (allowing duplicates)
    to a standard quadtree.
    """
    jobs = []
    intermediate_count = 0
    intermediates = defaultdict(list)
    final_count = 0
    for name in os.listdir(csvdir):
        if name.startswith("map_") and name.endswith(".csv"):
            final_count += 1
            jobs.append(pool.apply_async(csv_to_quadtree, (name, csvdir, quadtree_dir)))
        if name.startswith("submap_") and name.endswith(".csv"):
            intermediate_count += 1
            prefix, shard, suffix = name.split("_")
            basename, suffix = name.split(".")
            intermediates[shard].append(basename)
            submap_dir = os.path.join(quadtree_dir, f"submap_{shard}")
            if not os.path.isdir(submap_dir):
                os.mkdir(submap_dir)
            jobs.append(pool.apply_async(csv_to_quadtree, (name, csvdir, submap_dir)))

    # Run conversion jobs to completion
    def on_progress(converted, percent):
        if converted == 1:
            LOG.debug(f"  Converted 1 CSV to a quadtree ({percent:0.1%})")
        else:
            LOG.debug(f"  Converted {converted:,} CSVs to quadtrees ({percent:0.1%})")

    watch_jobs(jobs, on_progress=on_progress)
    csv_count = len(jobs)

    # Queue jobs to merge intermediates
    merge_jobs = []
    for shard, basenames in intermediates.items():
        submap_dir = os.path.join(quadtree_dir, f"submap_{shard}")
        map_dir = os.path.join(quadtree_dir, f"map_{shard}")
        merge_jobs.append(
            pool.apply_async(
                merge_quadtrees,
                (submap_dir, map_dir),
                {"remove_duplicates": False, "pattern": "submap*"},
            )
        )
        final_count += 1

    def on_merge_progress(merged, percent):
        LOG.debug(
            f"  Merged intermediate quadtrees to {merged:,}"
            f" quadtree{_s(merged)} ({percent:0.1%})"
        )

    watch_jobs(merge_jobs, on_progress=on_merge_progress)
    return (csv_count, intermediate_count, final_count)


def merge_quadtrees(quadtree_dir, shapes_dir, remove_duplicates=True, pattern="map*"):
    """Merge multiple quadtree files into one, removing duplicates."""
    quadtree_files = glob.glob(os.path.join(quadtree_dir, pattern))
    assert quadtree_files
    cmd = ["merge"]
    if remove_duplicates:
        cmd.append("-u")
    cmd += ["-o", shapes_dir]  # Output to shapes directory
    cmd += quadtree_files  # input files
    subprocess.run(cmd, check=True, capture_output=True)


def render_tiles(pool, shapes_dir, tiles_dir, max_zoom):
    """Render the tiles at all zoom levels, and the front-page 2x tile."""

    # Render tiles at all zoom levels
    tile_count = render_tiles_for_zoom_levels(pool, shapes_dir, tiles_dir, max_zoom)

    # Render front-page tile
    tile_count += render_tiles_for_zoom_levels(
        pool,
        shapes_dir,
        tiles_dir,
        max_zoom=0,
        tile_type="high-resolution tile",
        extra_args=("-T", "512"),  # Tile size 512 instead of default of 256
        suffix="@2x",  # Suffix for high-res variant images
    )

    return tile_count


def get_sync_plan(bucket_name, tiles_dir, bucket_prefix="tiles/"):
    """Compare S3 bucket and tiles directory to determine the sync plan."""

    # Get objects currently in the S3 bucket
    with Timer() as obj_timer:
        objects = get_current_objects(bucket_name, bucket_prefix)
    LOG.debug(
        f"Found {len(objects):,} existing tiles in bucket {bucket_name},"
        f" /{bucket_prefix} in {obj_timer.duration_s:0.1f} seconds"
    )

    # Determine what actions we are taking for each
    with Timer() as action_timer:
        actions, unchanged_count = get_sync_actions(tiles_dir, objects)
    LOG.debug(
        f"Completed sync actions in {action_timer.duration_s:0.1f} seconds,"
        f" {len(actions['upload']):,} new"
        f" tile{_s(len(actions['upload']))} to upload,"
        f" {len(actions['update']):,} changed"
        f" tile{_s(len(actions['update']))} to update,"
        f" {len(actions['delete']):,} orphaned"
        f" tile{_s(len(actions['delete']))} to delete,"
        f" and {unchanged_count:,} unchanged"
        f" tile{_s(unchanged_count)}"
    )

    return actions, unchanged_count


def sync_tiles(
    pool,
    plan,
    bucket_name,
    tiles_dir,
    max_zoom,
    raven_client,
    bucket_prefix="tiles/",
    delete_batch_size=100,
):
    """Execute the plan to sync the local tiles to S3 bucket objects."""

    result = {
        "tile_new": 0,
        "tile_changed": 0,
        "tile_deleted": 0,
        "tile_failed": 0,
    }

    # Queue the sync plan actions
    jobs = []
    for path in plan["upload"]:
        jobs.append(
            pool.apply_async(upload_file, (path, bucket_name, bucket_prefix, tiles_dir))
        )
    for path in plan["update"]:
        jobs.append(
            pool.apply_async(update_file, (path, bucket_name, bucket_prefix, tiles_dir))
        )
    total = len(plan["upload"]) + len(plan["update"])

    # Queue the delete actions in batches
    for paths in chunked(plan["delete"], delete_batch_size):
        total += len(paths)
        jobs.append(pool.apply_async(delete_files, (paths, bucket_name, bucket_prefix)))

    # Watch sync jobs until completion
    def on_success(job_result):
        nonlocal result
        tile_result, count = job_result
        result[tile_result] += count

    def on_error(exception):
        nonlocal result
        LOG.error(f"Exception while syncing: {exception}")
        result["tile_failed"] += 1  # Would be wrong if a delete fails

    def on_progress(jobs_complete, job_total):
        nonlocal result, total
        count = sum(result.values())
        percent = count / total
        LOG.debug(f"  Synced {count:,} file{_s(count)} ({percent:.1%})")

    watch_jobs(jobs, on_progress=on_progress, on_success=on_success, on_error=on_error)
    return result


def upload_status_file(bucket_name, runtime_data, bucket_prefix="tiles/"):
    """Upload the status file to S3"""

    data = {"updated": util.utcnow().isoformat()}
    data.update(runtime_data)
    s3_client().put_object(
        Body=dumps(data),
        Bucket=bucket_name,
        CacheControl="max-age=3600, public",
        ContentType="application/json",
        Key=bucket_prefix + "data.json",
    )


def export_to_csv(filename, csv_dir, tablename, row_limit=None, file_limit=None):
    """
    Export a datamap table to a CSV file.

    :param filename: An output file ending in .csv
    :param csv_dir: The output directory
    :param tablename: The name of the datamap table to export
    :param row_limit: The number of rows to fetch at a time
    :param file_limit: The number of output rows before rotating files
    :return: A tuple (rows exported, files created)

    Each database row is turned into 0 to 6 similar CSV rows by
    random_points(), based on how recently they were recorded.

    If file_limit is not reached, the output file will the filename.
    If file_limit is reached, the output files will have a serial number and
    be based on the filename. For example, "map.csv" will become "map_0001.csv",
    "map_0002.csv", etc.
    """
    stmt = text(
        """\
SELECT
`grid`, CAST(ROUND(DATEDIFF(CURDATE(), `modified`) / 30) AS UNSIGNED) as `num`
FROM {tablename}
WHERE `grid` > :grid
ORDER BY `grid`
LIMIT :limit
""".format(
            tablename=tablename
        ).replace(
            "\n", " "
        )
    )

    db = configure_db("ro", pool=False)
    min_grid = b""
    row_limit = row_limit or 200_000
    file_limit = file_limit or 10_000_000

    result_rows = 0
    file_path = os.path.join(csv_dir, filename)
    fd = open(file_path, "w")
    file_count = 1
    file_rows = 0
    orig_filename = filename
    orig_file_path = file_path
    assert filename.endswith(".csv")
    try:
        with db_worker_session(db, commit=False) as session:
            while True:
                result = session.execute(
                    stmt.bindparams(limit=row_limit, grid=min_grid)
                )
                rows = result.fetchall()
                result.close()
                if not rows:
                    break

                lines = []
                extend = lines.extend
                for row in rows:
                    lat, lon = decode_datamap_grid(row.grid)
                    extend(random_points(lat, lon, row.num))

                fd.writelines(lines)
                result_rows += len(lines)

                # Rotate the file when needed
                file_rows += len(lines)
                if result_rows >= file_limit:
                    fd.close()
                    file_count += 1
                    file_rows = 0
                    filename = "sub" + orig_filename.replace(
                        ".csv", f"_{file_count:04}.csv"
                    )
                    file_path = os.path.join(csv_dir, filename)
                    fd = open(file_path, "w")

                min_grid = rows[-1].grid
    finally:
        fd.close()

    if not file_rows:
        os.remove(file_path)
        file_count -= 1

    if file_count > 1:
        # Rename first file to serial CSV format
        filename = "sub" + orig_filename.replace(".csv", "_0001.csv")
        file_path = os.path.join(csv_dir, filename)
        os.rename(orig_file_path, file_path)

    db.close()
    return result_rows, file_count


def csv_to_quadtree(name, csv_dir, quadtree_dir):
    """
    Convert a CSV file into a quadtree.

    encode is from https://github.com/ericfischer/datamaps
    """
    input_path = os.path.join(csv_dir, name)
    with open(input_path, "rb") as csv_file:
        csv = csv_file.read()

    output_path = os.path.join(quadtree_dir, name.split(".")[0])
    cmd = ["encode", "-z13", "-o", output_path]  # Allow a single pixel at zoom level 13
    subprocess.run(cmd, input=csv, check=True, capture_output=True)


def render_tiles_for_zoom_levels(
    pool,
    shapes_dir,
    tiles_dir,
    max_zoom,
    tile_type="tile",
    **render_keywords,
):
    """Render tiles concurrently across the zoom level."""

    # Get the tile enumeration parameters
    tile_params = enumerate_tiles(shapes_dir, max_zoom)

    total = len(tile_params)
    LOG.debug(f"Rendering {total:,} {tile_type}{_s(tile_type)}...")

    # Create the directory structure
    create_tile_subfolders(tile_params, tiles_dir)

    # Create jobs to concurrently generate the tiles
    jobs = []
    keywords = {
        "tiles_dir": tiles_dir,
    }
    keywords.update(render_keywords)
    for params in tile_params:
        jobs.append(pool.apply_async(generate_tile, params, keywords))

    # Watch render jobs to completion
    def on_progress(rendered, percent):
        nonlocal tile_type
        LOG.debug(f"  Rendered {rendered:,} {tile_type}{_s(tile_type)} ({percent:.1%})")

    watch_jobs(jobs, on_progress=on_progress)
    return len(jobs)


def enumerate_tiles(shapes_dir, zoom):
    """Enumerate the zoom and tile positions combinations in the shapes quadtree."""
    cmd = [
        "enumerate",
        "-z",  # Zoom level...
        str(zoom),  # from 0 to this level, inclusive
        shapes_dir,  # the directory with the input quadtree
    ]
    complete = subprocess.run(cmd, check=True, capture_output=True)

    # Process into a tuple of 4-element tuples (shape_dir, zoom, tile x, tile y)
    output = []
    for line in complete.stdout.decode("utf8").splitlines():
        out = line.split()
        if out:
            assert len(out) == 4
            output.append(tuple(out))
    return tuple(output)


def create_tile_subfolders(tile_params, tiles_dir):
    """Create tile output subfolders if they do not exist."""

    folder_parts = set()
    for source_dir, zoom, tile_x, tile_y in tile_params:
        folder_parts.add((zoom, tile_x))

    for zoom, tile_x in folder_parts:
        folder = os.path.join(tiles_dir, zoom, tile_x)
        os.makedirs(folder, exist_ok=True)


def generate_tile(
    source_dir, zoom, tile_x, tile_y, tiles_dir, extra_args=None, suffix=""
):
    """Generate a space-optimized tile at a given zoom level and position."""
    render_cmd = [
        "render",
        "-B",  # Set basic display parameters...
        "12:0.0379:0.874",  # base zoom (default), brightness, ramp (less than defaults)
        "-c0088FF",  # fully saturated color, blue
        "-t0",  # Fully transparent with no data
        "-O",  # Tune for distance between points...
        "16:1600:1.5",  # Defaults for base, distance, ramp
        "-G",  # Gamma curve...
        "0.5",  # Default of square root
    ]
    if extra_args:
        render_cmd.extend(extra_args)
    render_cmd.extend((source_dir, zoom, tile_x, tile_y))

    pngquant_cmd = [
        "pngquant",
        "--speed",
        "3",  # Default speed
        "--quality",
        "65-95",  # JPEG-style quality, no compress if below 65%, aim for 95%
        "32",  # Output a 32-bit png
    ]

    # Emulate the shell command render | pngquant > out.png
    output_path = os.path.join(tiles_dir, zoom, tile_x, f"{tile_y}{suffix}.png")
    with open(output_path, "wb") as png:
        render = subprocess.Popen(
            render_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        pngquant = subprocess.Popen(
            pngquant_cmd,
            stdin=render.stdout,
            stdout=png,
            stderr=subprocess.DEVNULL,
        )
        render.stdout.close()
        pngquant.wait()


def s3_client():
    """
    Initialize the s3 bucket client.

    The S3 resource (boto3.resource("s3") has a more Pythonic API, but it also
    appears to increase memory usage with each call.
    """
    global S3_CLIENT
    if S3_CLIENT is None:
        session = boto3.session.Session()
        S3_CLIENT = session.client("s3")
    return S3_CLIENT


def reset_s3_client():
    """Clear the S3 client, to free memory."""
    global S3_CLIENT
    S3_CLIENT = None


def get_current_objects(bucket_name, bucket_prefix):
    """Get names, sizes, and MD5 signatures of objects in the bucket."""

    objects = {}
    more_to_fetch = True
    next_kwargs = {}
    while more_to_fetch:
        response = s3_client().list_objects_v2(
            Bucket=bucket_name, Prefix=bucket_prefix, **next_kwargs
        )
        # Process the objects
        for metadata in response["Contents"]:
            key = metadata["Key"]
            if key.endswith(".png"):
                name = key[len(bucket_prefix) :]
                md5 = metadata["ETag"].strip('"')
                size = metadata["Size"]
                objects[name] = (size, md5)

        # Are the more results to fetch?
        # The default is to return 1000 objects at a time
        more_to_fetch = response["IsTruncated"]
        if more_to_fetch:
            next_kwargs = {"ContinuationToken": response["NextContinuationToken"]}

    return objects


def get_sync_actions(tiles_dir, objects):
    """Determine the actions to take to sync tiles with S3 bucket."""
    actions = {
        "upload": [],
        "update": [],
        "delete": [],
    }
    unchanged_count = 0
    remaining_objects = set(objects.keys())

    for png in get_png_entries(tiles_dir):
        obj_name = png.path[len(tiles_dir) :].lstrip("/")
        if obj_name in remaining_objects:
            remaining_objects.remove(obj_name)

            # Check if size then md5 are different
            changed = True
            obj_size, obj_md5 = objects[obj_name]
            local_size = png.stat().st_size
            if local_size == obj_size:
                with open(png.path, "rb") as fd:
                    local_md5 = hashlib.md5(fd.read()).hexdigest()
                if local_md5 == obj_md5:
                    changed = False

            if changed:
                actions["update"].append(obj_name)
            else:
                unchanged_count += 1
        else:
            # New object
            actions["upload"].append(obj_name)

    # Any remaining objects should be deleted
    actions["delete"] = sorted(remaining_objects)
    return actions, unchanged_count


def upload_file(path, bucket_name, bucket_prefix, tiles_dir):
    send_file(path, bucket_name, bucket_prefix, tiles_dir)
    return "tile_new", 1


def update_file(path, bucket_name, bucket_prefix, tiles_dir):
    send_file(path, bucket_name, bucket_prefix, tiles_dir)
    return "tile_changed", 1


def send_file(path, bucket_name, bucket_prefix, tiles_dir):
    """Send the local file to the S3 bucket."""
    s3_client().upload_file(
        Filename=os.path.join(tiles_dir, path),
        Bucket=bucket_name,
        Key=bucket_prefix + path,
        ExtraArgs={
            "CacheControl": "max-age=3600, public",
            "ContentType": "image/png",
        },
    )


def delete_files(paths, bucket_name, bucket_prefix):
    """Delete multiple files from the S3 bucket."""
    delete_request = {
        "Objects": [{"Key": bucket_prefix + path} for path in paths],
        "Quiet": True,
    }
    resp = s3_client().delete_objects(Bucket=bucket_name, Delete=delete_request)
    if resp.get("Errors"):
        raise RuntimeError(f"Error deleting: {resp['Errors']}")
    return "tile_deleted", len(paths)


def get_png_entries(top):
    """Recursively find .png files in a folder"""
    for entry in os.scandir(top):
        if entry.is_dir():
            for subentry in get_png_entries(entry.path):
                yield subentry
        elif entry.name.endswith(".png"):
            yield entry


#
# Command-line entry points
#


def get_parser():
    """Return a command-line parser."""

    try:
        # How many CPUs can this process address?
        # Avaiable on some Unix systems, and in Docker image
        concurrency = len(os.sched_getaffinity(0))
    except AttributeError:
        # Fallback to the CPU count
        concurrency = os.cpu_count()
    parser = argparse.ArgumentParser(description="Generate and upload datamap tiles.")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Turn on verbose logging. Equivalent to setting LOCAL_DEV_ENV=1"
            " and LOGGING_LEVEL=debug"
        ),
    )
    parser.add_argument("--create", action="store_true", help="Create tiles")
    parser.add_argument("--upload", action="store_true", help="Upload tiles to S3")
    parser.add_argument(
        "--concurrency",
        type=int,
        choices=list(range(1, concurrency + 1)),
        default=concurrency,
        help=f"How many concurrent processes to use? (default {concurrency})",
    )
    parser.add_argument(
        "--output",
        help=(
            "Directory for generated tiles and working files. A temporary"
            " directory is created and used if omitted."
        ),
    )
    return parser


def check_bucket(bucket_name):
    """
    Check that we can write to a bucket.

    Returns (True, None) on success, (False, "fail message") if not writable

    Bucket existance check based on https://stackoverflow.com/a/47565719/10612
    """
    client = s3_client()

    # Test if we can see the bucket at all
    try:
        client.head_bucket(Bucket=bucket_name)
    except botocore.exceptions.ClientError as e:
        error_code = int(e.response["Error"]["Code"])
        if error_code == 403:
            return False, "Access forbidden"
        elif error_code == 404:
            return False, "Bucket does not exist"
        else:
            msg = (
                "Unknown error on head_bucket,"
                f" Code {e.response['Error']['Code']},"
                f" Message {e.response['Error']['Message']}"
            )
            return False, msg
    except botocore.exceptions.NoCredentialsError:
        return False, (
            "Unable to locate AWS credentials, see "
            "https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html"
        )

    # Create and delete a test file
    test_name = f"test-{uuid.uuid4()}"
    client.put_object(Bucket=bucket_name, Body=b"write test", Key=test_name)
    client.get_waiter("object_exists").wait(Bucket=bucket_name, Key=test_name)
    client.delete_object(Bucket=bucket_name, Key=test_name)
    client.get_waiter("object_not_exists").wait(Bucket=bucket_name, Key=test_name)

    return True, None


def main(_argv=None, _raven_client=None, _bucket_name=None):
    """
    Command-line entry point.

    :param _argv: Simulated sys.argv[1:] arguments for testing
    :param _raven_client: override Raven client for testing
    :param _bucket_name: override S3 bucket name for testing
    :return: A system exit code
    :rtype: int
    """

    # Parse the command line
    parser = get_parser()
    args = parser.parse_args(_argv)
    create = args.create
    upload = args.upload
    concurrency = args.concurrency
    verbose = args.verbose

    # Setup basic services
    if verbose:
        configure_logging(local_dev_env=True, logging_level="DEBUG")
    else:
        configure_logging()
    raven_client = configure_raven(
        transport="sync", tags={"app": "datamap"}, _client=_raven_client
    )

    # Check consistent output_dir, create, upload
    exit_early = 0
    output_dir = None
    if args.output:
        output_dir = os.path.abspath(args.output)
        tiles_dir = os.path.join(output_dir, "tiles")
        if not create and not os.path.isdir(tiles_dir):
            LOG.error(
                "The tiles subfolder of the --output directory should already"
                " exist when calling --upload without --create, to avoid"
                " deleting files from the S3 bucket.",
                tiles_dir=tiles_dir,
            )
            exit_early = 1
    else:
        if create and not upload:
            LOG.error(
                "The --output argument is required with --create but without"
                " --upload, since the temporary folder is removed at exit."
            )
            exit_early = 1

        if upload and not create:
            LOG.error(
                "The --output argument is required with --upload but without"
                " --create, to avoid deleting all tiles in the S3 bucket."
            )
            exit_early = 1

    # Exit early with help message if error or nothing to do
    if exit_early or not (create or upload):
        parser.print_help()
        return exit_early

    # Determine the S3 bucket name
    bucket_name = _bucket_name
    if not _bucket_name:
        bucket_name = settings("asset_bucket")
        if bucket_name:
            bucket_name = bucket_name.strip("/")

    # Check that the implied credentials are authorized to use the bucket
    if upload:
        if not bucket_name:
            LOG.error("Unable to determine upload bucket_name.")
            return 1
        else:
            works, fail_msg = check_bucket(bucket_name)
            if not works:
                LOG.error(
                    f"Bucket {bucket_name} can not be used for uploads: {fail_msg}"
                )
                return 1

    # Generate and upload the tiles
    success = True
    interrupted = False
    result = {}
    try:
        with Timer() as timer:
            if output_dir:
                result = generate(
                    output_dir,
                    bucket_name,
                    raven_client,
                    create=create,
                    upload=upload,
                    concurrency=concurrency,
                )
            else:
                with util.selfdestruct_tempdir() as temp_dir:
                    result = generate(
                        temp_dir,
                        bucket_name,
                        raven_client,
                        create=create,
                        upload=upload,
                        concurrency=concurrency,
                    )
    except KeyboardInterrupt:
        interrupted = True
        success = False
    except Exception:
        raven_client.captureException()
        success = False
        raise
    finally:
        if create and upload:
            task = "generation and upload"
        elif create:
            task = "generation"
        else:
            task = "upload"
        if interrupted:
            complete = "interrupted"
        elif success:
            complete = "complete"
        else:
            complete = "failed"
        final_log = structlog.get_logger("canonical-log-line")
        final_log.info(
            f"Datamap tile {task} {complete} in {timer.duration_s:0.1f} seconds.",
            success=success,
            duration_s=timer.duration_s,
            script_name="ichnaea.scripts.datamap",
            create=create,
            upload=upload,
            concurrency=concurrency,
            bucket_name=bucket_name,
            **result,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
