"""
pghoard

Copyright (c) 2015 Ohmu Ltd
See LICENSE for details
"""
from __future__ import print_function
from .common import lzma, default_log_format_str
from .errors import Error
from requests import Session
import argh
import logging
import os
import sys
import tarfile
import time
import warnings

warnings.filterwarnings("ignore", ".*", UserWarning, "argh.completion")


def create_pgdata_dir(pgdata):
    if not os.path.exists(pgdata):
        os.makedirs(pgdata)
    os.chmod(pgdata, 0o700)


class Restore(object):
    def __init__(self):
        self.storage = None
        self.parser = self.create_parser()
        self.log = logging.getLogger("PGHoardRestore")

    def create_parser(self):
        parser = argh.ArghParser()
        argh.add_commands(parser, [
            self.get_basebackup_http, self.list_basebackups_http,
            self.get_basebackup_s3, self.list_basebackups_s3,
        ])
        return parser

    @argh.arg("--host", help="pghoard repository host")
    @argh.arg("--port", help="pghoard repository port")
    @argh.arg("--site", help="pghoard site")
    @argh.arg("--basebackup", help="pghoard basebackup", required=True)
    @argh.arg("--target-dir", help="pghoard restore target 'pgdata' dir", required=True)
    def get_basebackup_http(self, basebackup, target_dir, host="localhost", port=16000, site="default"):
        self.storage = HTTPRestore(host, port, site, target_dir)
        self.get_basebackup(target_dir, basebackup)

    @argh.arg("--host", help="pghoard repository host")
    @argh.arg("--port", help="pghoard repository port")
    @argh.arg("--site", help="pghoard site")
    def list_basebackups_http(self, host="localhost", port=16000, site="default"):
        self.storage = HTTPRestore(host, port, site)
        self.storage.list_basebackups()

    @argh.arg("--aws-access-key-id", help="AWS Access Key ID [AWS_ACCESS_KEY_ID]", default=os.environ.get("AWS_ACCESS_KEY_ID"))
    @argh.arg("--aws-secret-access-key", help="AWS Secret Access Key [AWS_SECRET_ACCESS_KEY]", default=os.environ.get("AWS_SECRET_ACCESS_KEY"))
    @argh.arg("--region", help="AWS S3 region")
    @argh.arg("--bucket", help="AWS S3 bucket name", required=True)
    @argh.arg("--site", help="pghoard site")
    @argh.arg("--basebackup", help="pghoard basebackup", required=True)
    @argh.arg("--target-dir", help="pghoard restore target 'pgdata' dir", required=True)
    def get_basebackup_s3(self, aws_access_key_id, aws_secret_access_key, bucket, basebackup, target_dir, region="eu-west-1", site="default"):
        self.storage = S3Restore(aws_access_key_id, aws_secret_access_key, region, bucket, site, target_dir)
        self.get_basebackup(target_dir, basebackup)

    @argh.arg("--aws-access-key-id", help="AWS Access Key ID [AWS_ACCESS_KEY_ID]", default=os.environ.get("AWS_ACCESS_KEY_ID"))
    @argh.arg("--aws-secret-access-key", help="AWS Secret Access Key [AWS_SECRET_ACCESS_KEY]", default=os.environ.get("AWS_SECRET_ACCESS_KEY"))
    @argh.arg("--region", help="AWS S3 region")
    @argh.arg("--bucket", help="AWS S3 bucket name", required=True)
    @argh.arg("--site", help="pghoard site")
    def list_basebackups_s3(self, aws_access_key_id, aws_secret_access_key, bucket, region="eu-west-1", site="default"):
        self.storage = S3Restore(aws_access_key_id, aws_secret_access_key, region, bucket, site)
        self.storage.list_basebackups()

    def get_basebackup(self, pgdata, basebackup):
        create_pgdata_dir(pgdata)

        basebackup_path, wal_segment = self.storage.get_basebackup_file(basebackup)
        tar = tarfile.TarFile(fileobj=lzma.LZMAFile(basebackup_path, "rb"))
        tar.extractall(pgdata)

        for timeline in self.storage.list_timelines():
            self.storage.get_timeline_file(timeline)

        wal_segment_no = int(wal_segment, 16)
        while self.storage.get_wal_segment(wal_segment):
            # Note this does not take care of timelines/older PGs
            wal_segment_no += 1
            wal_segment = hex(wal_segment_no)[2:].upper().zfill(24)

        print("Basebackup complete, you can start PostgreSQL by running pg_ctl -D %s start" % pgdata)

    def run(self):
        argh.dispatch(self.parser)


class ObjectStore(object):
    def __init__(self, storage, site, pgdata):
        self.log = logging.getLogger(self.__class__.__name__)
        self.storage = storage
        self.site = site
        self.pgdata = pgdata

    def list_basebackups(self):
        result = self.storage.list_path(self.site + "/basebackup/")
        line = "Available %r basebackups:" % self.site
        print(line)
        print("=" * len(line))
        print("basebackup\t\t\tsize\tlast_modified\t\t\tmetadata")
        for r in result:
            print("%s\t%s\t%s\t%s" % (r["name"], r["size"], r["last_modified"], r["metadata"]))

    def list_timelines(self):
        result = self.storage.list_path(self.site + "/timeline/")
        if not result:
            return []
        return [r.key for r in result]

    def get_basebackup_file(self, basebackup):
        metadata = self.storage.get_metadata_for_key(basebackup)
        basebackup_path = os.path.join(self.pgdata, "base.tar.xz")
        self.storage.get_contents_to_file(basebackup, basebackup_path)
        return basebackup_path, metadata["start_wal_segment"]

    def get_wal_segment(self, wal_segment):
        key = self.site + "/xlog/" + wal_segment
        try:
            wal_data, _ = self.storage.get_contents_to_string(key)
        except Exception:  # XXX: ignore "not found" errors, not others; pylint: disable=W0703
            self.log.exception("Problem fetching: %r", wal_segment)
            return False
        decompressor = lzma.LZMADecompressor()
        decompressed_data = decompressor.decompress(wal_data)
        with open(os.path.join(self.pgdata, "pg_xlog", wal_segment), "wb") as fp:
            fp.write(decompressed_data)
        return True


class S3Restore(ObjectStore):
    def __init__(self, aws_access_key_id, aws_secret_access_key, region, bucket, site, pgdata=None):
        from .object_storage.s3 import S3Transfer
        storage = S3Transfer(aws_access_key_id, aws_secret_access_key, region, bucket)
        ObjectStore.__init__(self, storage, site, pgdata)


def store_response_to_file(filepath, response):
    decompressor = lzma.LZMADecompressor()
    with open(filepath, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:  # filter out keep-alive new chunks
                f.write(decompressor.decompress(chunk))


class HTTPRestore(object):
    def __init__(self, host, port, site, pgdata=None):
        self.log = logging.getLogger("HTTPRestore")
        self.host = host
        self.port = port
        self.site = site
        self.pgdata = pgdata
        self.session = Session()

    def _list_basebackups(self):
        uri = "http://" + self.host + ":" + str(self.port) + "/" + self.site + "/basebackups"
        return self.session.get(uri)

    def list_basebackups(self):
        result = self._list_basebackups()
        line = "Available %r basebackups:" % self.site
        print(line)
        print("=" * len(line))
        print("basebackup\t\tsize")
        for basebackup, values in result.json()["basebackups"].items():
            print("%s\t%s" % (basebackup, values["size"]))

    def list_timelines(self):
        uri = "http://" + self.host + ":" + str(self.port) + "/" + self.site + "/timelines"
        result = self.session.get(uri)
        if result.status_code == 200:
            return result.json()["timelines"]
        return []

    def get_basebackup_file(self, basebackup):
        uri = "http://" + self.host + ":" + str(self.port) + "/" + self.site + "/basebackups/" + basebackup
        response = self.session.get(uri, stream=True)
        if response.status_code != 200:
            raise Error("Incorrect basebackup: %{!r} or site: {!r} defined".format(basebackup, self.site))
        basebackup_path = os.path.join(self.pgdata, "base.tar.xz")
        store_response_to_file(basebackup_path, response)
        return basebackup_path, response.headers["x-pghoard-start_wal_segment"]

    def get_timeline_file(self, timeline):
        start_time = time.time()
        uri = "http://" + self.host + ":" + str(self.port) + "/" + self.site + "/timelines/" + timeline
        response = self.session.get(uri, stream=True)
        store_response_to_file(os.path.join(self.pgdata, "pg_xlog", timeline), response)
        self.log.debug("Got timeline: %r, status_code: %r took: %.2fs", timeline, response.status_code,
                       time.time() - start_time)
        return response.status_code == 200

    def get_wal_segment(self, wal_segment):
        start_time = time.time()
        uri = "http://" + self.host + ":" + str(self.port) + "/" + self.site + "/xlog/" + wal_segment
        response = self.session.get(uri, stream=True)
        store_response_to_file(os.path.join(self.pgdata, "pg_xlog", wal_segment), response)
        self.log.debug("Got WAL: %r, status_code: %r took: %.2fs", wal_segment, response.status_code,
                       time.time() - start_time)
        return response.status_code == 200


def main():
    logging.basicConfig(level=logging.INFO, format=default_log_format_str)
    restore = Restore()
    return restore.run()


if __name__ == "__main__":
    sys.exit(main())