import json
import urllib
import logging

import boto
import boto.s3.connection

from nose.tools import eq_ as eq
try:
    from itertools import izip_longest as zip_longest
except ImportError:
    from itertools import zip_longest

from rgw_multi.multisite import *

log = logging.getLogger(__name__)

def check_object_eq(k1, k2, check_extra = True):
    assert k1
    assert k2
    log.debug('comparing key name=%s', k1.name)
    eq(k1.name, k2.name)
    eq(k1.metadata, k2.metadata)
    # eq(k1.cache_control, k2.cache_control)
    eq(k1.content_type, k2.content_type)
    # eq(k1.content_encoding, k2.content_encoding)
    # eq(k1.content_disposition, k2.content_disposition)
    # eq(k1.content_language, k2.content_language)
    eq(k1.etag, k2.etag)
    eq(k1.last_modified, k2.last_modified)
    if check_extra:
        eq(k1.owner.id, k2.owner.id)
        eq(k1.owner.display_name, k2.owner.display_name)
    # eq(k1.storage_class, k2.storage_class)
    eq(k1.size, k2.size)
    eq(k1.version_id, k2.version_id)
    # eq(k1.encrypted, k2.encrypted)

def make_request(conn, method, bucket, key, query_args, headers):
    result = conn.make_request(method, bucket=bucket, key=key, query_args=query_args, headers=headers)
    if result.status / 100 != 2:
        raise boto.exception.S3ResponseError(result.status, result.reason, result.read())
    return result

def dump_json(o):
    return json.dumps(o, indent=4)

def append_query_arg(s, n, v):
    if not v:
        return s
    nv = '{n}={v}'.format(n=n, v=v)
    if not s:
        return nv
    return '{s}&{nv}'.format(s=s, nv=nv)

class MDSearch:
    def __init__(self, conn, bucket_name, query, query_args = None, marker = None):
        self.conn = conn
        self.bucket_name = bucket_name or ''
        self.query = query
        self.query_args = query_args
        self.max_keys = None
        self.marker = marker

    def search(self):
        q = self.query or ''
        query_args = append_query_arg(self.query_args, 'query', urllib.quote_plus(q))
        if self.max_keys is not None:
            query_args = append_query_arg(query_args, 'max-keys', self.max_keys)
        if self.marker:
            query_args = append_query_arg(query_args, 'marker', self.marker)

        query_args = append_query_arg(query_args, 'format', 'json')

        headers = {}

        result = make_request(self.conn, "GET", bucket=self.bucket_name, key='', query_args=query_args, headers=headers)
        return json.loads(result.read())


class ESZoneBucket:
    def __init__(self, zone_conn, name, conn):
        self.zone_conn = zone_conn
        self.name = name
        self.conn = conn

        self.bucket = boto.s3.bucket.Bucket(name=name)

    def get_all_versions(self):

        marker = None
        is_done = False

        l = []

        while not is_done:
            req = MDSearch(self.conn, self.name, 'bucket == ' + self.name, marker=marker)

            result = req.search()

            for entry in result['Objects']:
                k = boto.s3.key.Key(self.bucket, entry['Key'])

                k.version_id = entry['Instance']
                k.etag = entry['ETag']
                k.owner = boto.s3.user.User(id=entry['Owner']['ID'], display_name=entry['Owner']['DisplayName'])
                k.last_modified = entry['LastModified']
                k.size = entry['Size']
                k.content_type = entry['ContentType']
                k.versioned_epoch = entry['VersionedEpoch']

                k.metadata = {}
                for e in entry['CustomMetadata']:
                    k.metadata[e['Name']] = e['Value']

                l.append(k)

            is_done = (result['IsTruncated'] == "false")
            marker = result['Marker']

        l.sort(key = lambda l: (l.name, -l.versioned_epoch))

        for k in l:
            yield k




class ESZone(Zone):
    def __init__(self, name, es_endpoint, zonegroup = None, cluster = None, data = None, zone_id = None, gateways = []):
        self.es_endpoint = es_endpoint
        super(ESZone, self).__init__(name, zonegroup, cluster, data, zone_id, gateways)

    def is_read_only(self):
        return True

    def tier_type(self):
        return "elasticsearch"

    def create(self, cluster, args = None, check_retcode = True):
        """ create the object with the given arguments """

        if args is None:
            args = ''

        tier_config = ','.join([ 'endpoint=' + self.es_endpoint, 'explicit_custom_meta=false' ])

        args += [ '--tier-type', self.tier_type(), '--tier-config', tier_config ] 

        return self.json_command(cluster, 'create', args, check_retcode=check_retcode)

    def has_buckets(self):
        return False

    class Conn(ZoneConn):
        def __init__(self, zone, credentials):
            super(ESZone.Conn, self).__init__(zone, credentials)

        def get_bucket(self, bucket_name):
            return ESZoneBucket(self, bucket_name, self.conn)

        def create_bucket(self, name):
            # should not be here, a bug in the test suite
            log.critical('Conn.create_bucket() should not be called in ES zone')
            assert False

        def check_bucket_eq(self, zone_conn, bucket_name):
            assert(zone_conn.zone.tier_type() == "rados")

            log.info('comparing bucket=%s zones={%s, %s}', bucket_name, self.name, self.name)
            b1 = self.get_bucket(bucket_name)
            b2 = zone_conn.get_bucket(bucket_name)

            log.debug('bucket1 objects:')
            for o in b1.get_all_versions():
                log.debug('o=%s', o.name)
            log.debug('bucket2 objects:')
            for o in b2.get_all_versions():
                log.debug('o=%s', o.name)

            for k1, k2 in zip_longest(b1.get_all_versions(), b2.get_all_versions()):
                if k1 is None:
                    log.critical('key=%s is missing from zone=%s', k2.name, self.self.name)
                    assert False
                if k2 is None:
                    log.critical('key=%s is missing from zone=%s', k1.name, zone_conn.name)
                    assert False

                check_object_eq(k1, k2)


            log.info('success, bucket identical: bucket=%s zones={%s, %s}', bucket_name, self.name, zone_conn.name)

            return True

    def get_conn(self, credentials):
        return self.Conn(self, credentials)


