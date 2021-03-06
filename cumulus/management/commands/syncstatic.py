import datetime
import optparse
import os
from urlparse import urlparse

import cloudfiles

from django.conf import settings
from django.core.management.base import BaseCommand
from cumulus.settings import CUMULUS
from cumulus.storage import sync_headers

class Command(BaseCommand):
    help = "Synchronizes static media to cloud files."

    option_list = BaseCommand.option_list + (
        optparse.make_option('-w', '--wipe',
            action='store_true', dest='wipe', default=False,
            help="Wipes out entire contents of container first."),
        optparse.make_option('-t', '--test-run',
            action='store_true', dest='test_run', default=False,
            help="Performs a test run of the sync."),
        optparse.make_option('-a', '--add-only',
            action='store_true', dest='add_only', default=False,
            help="Syncs all files but without doing updates or deletes."),
        optparse.make_option('-n', '--no-delete',
            action='store_true', dest='no_delete', default=False,
            help="Syncs all files but without doing deletes."),
    )

    # settings from cumulus.settings
    USERNAME         = CUMULUS['USERNAME']
    API_KEY          = CUMULUS['API_KEY']
    STATIC_CONTAINER = CUMULUS['STATIC_CONTAINER']
    USE_SERVICENET   = CUMULUS['SERVICENET']
    FILTER_LIST      = CUMULUS['FILTER_LIST']
    AUTH_URL         = CUMULUS['AUTH_URL']

    # paths
    DIRECTORY        = os.path.abspath(settings.STATIC_ROOT)
    STATIC_URL       = settings.STATIC_URL

    if not DIRECTORY.endswith('/'):
        DIRECTORY = DIRECTORY + '/'


    STATIC_URL = urlparse(STATIC_URL)[2] # Path component is 2

    if STATIC_URL.startswith('/'):
        STATIC_URL = STATIC_URL[1:]

    local_object_names = []
    cloudfile_info_list = []

    create_count = 0
    upload_count = 0
    update_count = 0
    skip_count = 0
    delete_count = 0
    conn = None
    container = None

    def handle(self, *args, **options):
        self.wipe = options.get('wipe')
        self.test_run = options.get('test_run')
        self.add_only = options.get('add_only')
        self.no_delete = options.get('no_delete')
        self.verbosity = int(options.get('verbosity'))
        self.sync_files()

    def sync_files(self):
        self.conn = cloudfiles.get_connection(username = self.USERNAME,
                                              api_key = self.API_KEY,
                                              authurl = self.AUTH_URL,
                                              servicenet=self.USE_SERVICENET)

        try:
            self.container = self.conn.get_container(self.STATIC_CONTAINER)
        except cloudfiles.errors.NoSuchContainer:
            self.container = self.conn.create_container(self.STATIC_CONTAINER)

        if not self.container.is_public():
            self.container.make_public()

        # if -w option is provided, wipe out the contents of the container
        if self.wipe:
            if self.test_run:
                print "Wipe would delete %d objects." % self.container.object_count
            else:
                print "Deleting %d objects..." % self.container.object_count
                for cloud_obj in self.container.get_objects():
                    self.container.delete_object(cloud_obj.name)
                    if self.verbosity > 1:
                        print "Deleted %s" % cloud_obj.name

        if self.verbosity > 1:
            print "Retreiving cloud file metadata"
        self.cloudfile_info_list = self.container.list_objects_info()

        # walk through the directory, creating or updating files on the cloud
        os.path.walk(self.DIRECTORY, self.upload_files, "foo")

        # remove any files on remote that don't exist locally
        if (not self.add_only) and (not self.no_delete):
            self.delete_files()

        # print out the final tally to the cmd line
        self.update_count = self.upload_count - self.create_count
        print
        if self.test_run:
            print "Test run complete with the following results:"
        print "Skipped %d. Created %d. Updated %d. Deleted %d." % (
            self.skip_count, self.create_count, self.update_count, self.delete_count)

        if self.verbosity > 1:
            print "CDN public uri:           %s" % self.container.public_uri()
            print "CDN public ssl uri:       %s" % self.container.public_ssl_uri()
            print "CDN public streaming uri: %s" % self.container.public_streaming_uri()
            print "CDN TTL: %s" % self.container.cdn_ttl
            print "CDN Size: %s" % self.container.size_used

    def upload_files(self, arg, dirname, names):

        # upload or skip items
        for item in names:
            if item in self.FILTER_LIST:
                continue # Skip files we don't want to sync

            file_path = os.path.join(dirname, item)
            if os.path.isdir(file_path):
                continue # Don't try to upload directories

            object_name = self.STATIC_URL + file_path.split(self.DIRECTORY)[1]
            self.local_object_names.append(object_name)

            # check if the metadata for our object is on the server
            obj_info = next((o for o in self.cloudfile_info_list if o['name'] == object_name), None)

            if not obj_info:
                # it is not on the server yet, we will create the object
                if not self.test_run:
                    cloud_obj = self.container.create_object(object_name)
                self.create_count += 1
            else:
                if self.add_only:
                    continue

                # check if it needs to be re-uploaded
                cloud_datetime = (obj_info['last_modified'] and
                                  datetime.datetime.strptime(
                                    obj_info['last_modified'],
                                    "%Y-%m-%dT%H:%M:%S.%f",
                                  ) or None)
                local_datetime = datetime.datetime.utcfromtimestamp(
                                                   os.stat(file_path).st_mtime)
                if cloud_datetime and local_datetime < cloud_datetime:
                    self.skip_count += 1
                    if self.verbosity > 1:
                        print "Skipped %s: not modified." % object_name
                    continue
                # check if the hash matches
                with file(file_path) as source:
                    if obj_info['hash'] == cloudfiles.Object.compute_md5sum(source):
                        if self.verbosity > 1:
                            print "Skipped %s: not modified (checksum)." % object_name
                        continue
                # we will have to update it, grab the object
                cloud_obj = self.container.get_object(object_name)

            # upload the file content
            if not self.test_run:
                cloud_obj.load_from_filename(file_path)
                sync_headers(cloud_obj)
            self.upload_count += 1
            if self.verbosity > 1:
                print "Uploaded", cloud_obj.name

    def delete_files(self):
        # remove any objects on the cloud that don't exist locally
        for cloud_name in self.container.list_objects():
            if cloud_name not in self.local_object_names:
                self.delete_count += 1
                if self.verbosity > 1:
                    print "Deleted %s" % cloud_name
                if not self.test_run:
                    self.container.delete_object(cloud_name)
