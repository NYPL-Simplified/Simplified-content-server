import json
import os
import subprocess
import tempfile
import urllib
from nose.tools import set_trace
from illustrated import GutenbergIllustratedDataProvider
from core.coverage import CoverageProvider
from core.model import (
    get_one,
    DataSource,
    Hyperlink,
    Identifier,
    LicensePool,
    Representation,
    Resource,
)
from core.s3 import S3Uploader

class GutenbergEPUBCoverageProvider(CoverageProvider):
    """Upload a text's epub to S3.

    Eventually this will generate the epub from scratch before
    uploading it.
    """

    def __init__(self, _db, workset_size=5, mirror_uploader=S3Uploader):
        data_directory = os.environ['DATA_DIRECTORY']

        self.gutenberg_mirror = os.path.join(
            data_directory, "Gutenberg", "gutenberg-mirror") + "/"
        self.epub_mirror = os.path.join(
            data_directory, "Gutenberg", "gutenberg-epub") + "/"

        input_source = DataSource.lookup(_db, DataSource.GUTENBERG)
        self.output_source = DataSource.lookup(
            _db, DataSource.GUTENBERG_EPUB_GENERATOR)        
        if callable(mirror_uploader):
            mirror_uploader = mirror_uploader()
        self.uploader = mirror_uploader

        super(GutenbergEPUBCoverageProvider, self).__init__(
            self.output_source.name, input_source, self.output_source,
            workset_size=workset_size)

    def process_edition(self, edition):
        identifier_obj = edition.primary_identifier
        epub_path = self.epub_path_for(identifier_obj)
        if not epub_path:
            return False
        license_pool = get_one(
            self._db, LicensePool, identifier_id=identifier_obj.id)

        url = self.uploader.book_url(identifier_obj, 'epub')
        link, new = license_pool.add_link(
            Hyperlink.OPEN_ACCESS_DOWNLOAD, url, self.output_source,
            Representation.EPUB_MEDIA_TYPE, None, epub_path)
        representation = link.resource.representation
        representation.mirror_url = url
        self.uploader.mirror_one(representation)
        return True

    def epub_path_for(self, identifier):
        """Find the path to the best EPUB for the given identifier."""
        if identifier.type != Identifier.GUTENBERG_ID:
            return None
        epub_directory = os.path.join(
            self.epub_mirror, identifier.identifier)
        if not os.path.exists(epub_directory):
            return None
        files = os.listdir(epub_directory)
        epub_filename = self.best_epub_in(files)
        if not epub_filename:
            return None
        return os.path.join(epub_directory, epub_filename)

    @classmethod
    def best_epub_in(cls, files):
        """Find the best EPUB in the given file list."""
        without_images = None
        with_images = None
        for i in files:
            if not i.endswith('.epub'):
                continue
            if i.endswith('-images.epub'):
                with_images = i
                break
            elif not without_images:
                without_images = i
        return with_images or without_images


class GutenbergIllustratedCoverageProvider(CoverageProvider):

    DESTINATION_DIRECTORY = "Gutenberg Illustrated"
    FONT_FILENAME = "AvenirNext-Bold-14.vlw"

    # An image smaller than this won't be turned into a Gutenberg
    # Illustrated cover--it's most likely too small to make a good
    # cover.
    IMAGE_CUTOFF_SIZE = 10 * 1024

    # Information about the images generated by Gutenberg Illustrated.
    MEDIA_TYPE = "image/png"
    IMAGE_HEIGHT = 300
    IMAGE_WIDTH = 200

    def __init__(self, _db, binary_path=None,
                 workset_size=5):

        data_directory = os.environ['DATA_DIRECTORY']
        binary_path = binary_path or os.environ['GUTENBERG_ILLUSTRATED_BINARY_PATH']

        self.gutenberg_mirror = os.path.join(
            data_directory, DataSource.GUTENBERG, "gutenberg-mirror") + "/"
        self.file_list = os.path.join(self.gutenberg_mirror, "ls-R")
        self.binary_path = binary_path
        binary_directory = os.path.split(self.binary_path)[0]
        self.font_path = os.path.join(
            binary_directory, 'data', self.FONT_FILENAME)
        self.output_directory = os.path.join(
            data_directory, self.DESTINATION_DIRECTORY) + "/"

        input_source = DataSource.lookup(_db, DataSource.GUTENBERG)
        self.output_source = DataSource.lookup(
            _db, DataSource.GUTENBERG_COVER_GENERATOR)

        super(GutenbergIllustratedCoverageProvider, self).__init__(
            "Gutenberg Illustrated", input_source, self.output_source,
            workset_size=workset_size)

        # Load the illustration lists from the Gutenberg ls-R file.
        self.illustration_lists = dict()
        file_list = open(self.file_list)
        for (gid, illustrations) in GutenbergIllustratedDataProvider.illustrations_from_file_list(
            file_list):
            if gid not in self.illustration_lists:
                self.illustration_lists[gid] = illustrations
        file_list.close()

        self.uploader = S3Uploader()

    def apply_size_filter(self, illustrations):
        large_enough = []
        for i in illustrations:
            path = os.path.join(self.gutenberg_mirror, i)
            if not os.path.exists(path):
                print "ERROR: could not find illustration %s" % path
                continue
            file_size = os.stat(path).st_size
            if file_size < self.IMAGE_CUTOFF_SIZE:
                #print "INFO: %s is only %d bytes, not using it." % (
                #    path, file_size)
                continue
            large_enough.append(i)
        return large_enough

    def process_edition(self, edition):
        data = GutenbergIllustratedDataProvider.data_for_edition(edition)
        data_source = DataSource.lookup(
            self._db, DataSource.GUTENBERG_COVER_GENERATOR)

        identifier_obj = edition.primary_identifier
        identifier = identifier_obj.identifier
        print "[ILLUSTRATED]", identifier_obj
        if identifier not in self.illustration_lists:
            # No illustrations for this edition. Nothing to do.
            print "[ILLUSTRATED] No illustrations."
            return True

        data['identifier'] = identifier
        illustrations = self.illustration_lists[identifier]

        # The size filter is time-consuming, so we apply it here, when
        # we know we're going to generate covers for this particular
        # book, rather than ahead of time.
        illustrations = self.apply_size_filter(illustrations)

        if not illustrations:
            # All illustrations were filtered out. Nothing to do.
            print "[ILLUSTRATED] All illustrations filtered out."
            return True

        # There is at least one cover available for this book.
        edition.no_known_cover = False

        data['illustrations'] = illustrations
        
        # Write the input to a temporary file.
        input_fh = tempfile.NamedTemporaryFile()
        json.dump(data, input_fh)
        input_fh.flush()

        # Make sure the output directory exists.
        if not os.path.exists(self.output_directory):
            os.makedirs(self.output_directory)

        args = self.args_for(input_fh.name)
        output_fh = tempfile.NamedTemporaryFile()
        try:
            subprocess.call(args, stdout=output_fh)
        except Exception, e:
            raise OSError(
                "Could not invoke subprocess %s. Original error: %s" % (
                " ".join(args), str(e)))

        output_capture = open(output_fh.name)
        print output_capture.read()
        output_capture.close()

        # We're done with the temporary files.
        input_fh.close()
        output_fh.close()

        # Associate 'cover' resources with the identifier
        output_directory = os.path.join(
            self.output_directory, identifier)

        license_pool = get_one(
            self._db, LicensePool, identifier_id=identifier_obj.id)
        to_upload = []
        if os.path.exists(output_directory):
            candidates = os.listdir(output_directory)            
        else:
            # All the potential images were filtered so the directory
            # was never created. Skip the upload step altogether.
            candidates = []

        for filename in candidates:
            if not filename.endswith('.png'):
                # Random unknown junk which we won't be uploading.
                continue
            path = os.path.join(output_directory, filename)

            # Load each generated image into the database as a
            # resource.

            # TODO: list directory before generation and only upload
            # images that changed during generation. Don't remove
            # images that were removed (e.g. because cutoff changed)
            # because people may still be using them.

            url = self.uploader.cover_image_url(
                data_source, identifier_obj, filename)
            link, new = license_pool.add_link(
                Hyperlink.IMAGE, url, self.output_source,
                "image/png", None, path)
            r = link.resource.representation
            r.mirror_url = url
            to_upload.append(r)

        self.uploader.mirror_batch(to_upload)
        print "[ILLUSTRATED] Uploaded %d resources." % len(to_upload)
        return True

    def args_for(self, input_path):
        """The command-line args to make covers out of a given directory."""
        return [self.binary_path, self.gutenberg_mirror, self.output_directory,
                input_path, self.font_path, self.font_path]
