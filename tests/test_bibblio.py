import json
import re
from datetime import datetime
from nose.tools import (
    assert_raises,
    eq_,
    set_trace,
)
from random import choice
from urllib import quote

from . import (
    DatabaseTest,
    sample_data,
)

from ..core.coverage import (
    CoverageFailure,
    WorkCoverageRecord,
)
from ..core.model import (
    ConfigurationSetting,
    DataSource,
    DeliveryMechanism,
    ExternalIntegration,
    Hyperlink,
    Identifier,
    LicensePoolDeliveryMechanism,
    Representation,
    RightsStatus,
)
from ..core.util.epub import EpubAccessor
from ..core.util.http import BadResponseException

from ..config import (
    CannotLoadConfiguration,
    Configuration,
)

from ..bibblio import (
    BibblioAPI,
    BibblioCoverageProvider,
    EpubFilter,
)


class MockBibblioAPI(object):

    def __init__(self, error=None):
        self.error = None

    def create_content_item(self, identifier):
        if self.error:
            raise self.error

        return json.loads("""{
          "catalogueId": "9e904824-5f98-4281-99be-931a8d68854e",
          "contentItemId": "510b1ee0-bede-4e24-a379-6a387f2dbb64",
          "name": "Air Pollution",
          "url": "http://example.com/path/to/content-item",
          "text": "Air pollution consists of chemicals or particles in the air that can harm the health of humans, animals, and plants. It also damages buildings. Pollutants in the air take many forms. They can be gases, solid particles, or liquid droplets. Sources of Air Pollution Pollution enters the Earth's atmosphere in many different ways. Most air pollution is created by people, taking the form of emissions from factories, cars, planes, or aerosol cans. [...]",
          "headline": "One of the most rapidly developing nations in the world also has one of the highest rates of air pollution.",
          "description": "Air pollution consists of chemicals or particles in the air that can harm the health of humans, animals, and plants. It also damages buildings.",
          "keywords": ["environment", "atmosphere"],
          "learningResourceType": "encyclopedia article",
          "thumbnail": {
            "contentUrl": "http://example.com/path/to/thumbnail"
          },
          "image": {
            "contentUrl": "http://example.com/full-size/air-pollution.jpg"
          },
          "moduleImage": {
            "contentUrl": "http://example.com/thumbnails/air-pollution-300x300.jpg"
          },
          "video": {
            "embedUrl": "http://example.com/embed/video-id"
          },
          "dateCreated": "2011-04-04T11:42:17.082Z",
          "dateModified": "2011-04-04T11:42:17.082Z",
          "datePublished": "2011-04-04T11:42:17.082Z",
          "provider": { "name": "Distribution Inc." },
          "publisher": { "name": "Publication Inc." }
        }""")


class TestBibblioAPI(DatabaseTest):

    def test_from_config(self):
        # When nothing has been configured, an error is raised.
        assert_raises(
            CannotLoadConfiguration, BibblioAPI.from_config, self._db
        )

        # When there's only a partial configuration, None is returned.
        integration = self._external_integration(
            ExternalIntegration.BIBBLIO, goal=ExternalIntegration.METADATA_GOAL
        )
        integration.username = 'user'
        assert_raises(
            CannotLoadConfiguration, BibblioAPI.from_config, self._db
        )

        integration.username = None
        integration.password = 'pass'
        assert_raises(
            CannotLoadConfiguration, BibblioAPI.from_config, self._db
        )

        # A full configuration, the API is created.
        integration.username = 'user'
        result = BibblioAPI.from_config(self._db)
        eq_(True, isinstance(result, BibblioAPI))
        eq_('user', result.client_id)
        eq_('pass', result.client_secret)

    def test_timestamp(self):
        item = {'name' : 'banana'}
        expected_format = re.compile('\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z')
        now = datetime.utcnow()

        # Adds a 'dateModified' timestamp to a dictionary / JSON object
        result = BibblioAPI.set_timestamp(item)
        assert 'dateModified' in result
        # It's a string.
        assert isinstance(result['dateModified'], basestring)
        assert result['dateModified'] > (now.isoformat() + 'Z')

        assert 'dateCreated' not in result

        # Adds 'dateModified' and 'dateCreated' timestamps when specified
        result = BibblioAPI.set_timestamp(item, create=True)
        assert 'dateCreated' in result
        assert 'dateModified' in result
        eq_(result['dateCreated'], result['dateModified'])
        assert isinstance(result['dateCreated'], basestring)
        assert result['dateCreated'] > (now.isoformat() + 'Z')


class TestEpubFilter(object):


    class MockEpubFilter(EpubFilter):

        SPINE_IDREFS = set(['pub-data', 'cover'])

        FILTERED_PHRASES = [
            'We left school\.? (we)? (do)? lurk late',
            'Seven (at the)+ Golden Shovel',
            'sin',
            'we\s*',
            '\n        ',
        ]

    def test_filter_spine_idrefs(self):
        result = self.MockEpubFilter.filter_spine_idrefs([])
        eq_(0, len(result))

        idrefs = ['banana', 'pub-data', 'elephant', 'chapter-3']
        result = self.MockEpubFilter.filter_spine_idrefs(idrefs)
        assert 'pub-data' not in result
        eq_(3, len(result))

    def test_phrase_regex(self):
        result = self.MockEpubFilter.phrase_regex(
            'We left school(.)? (we)? (do)? lurk late'
        )

        # The result is regex.
        eq_(None, result.match('We left school.'))
        assert result.match('We left school. lurk late.')

        # The result is case insensitive.
        assert result.match('WE LEFT SCHOOL. LURK LATE.')

        # The result accounts for whitespace.
        assert result.match('we left school           \nwe do lurk late')

    def test_filter(self):
        original = """The Pool Players.
        Seven at the Golden Shovel.

        We real cool. We
        Left school. We

        Lurk late. We
        Strike straight. We

        Sing sin. We
        Thin gin. We

        Jazz June. We
        Die soon."""

        # Phrases from MockEpubFilter.FILTERED_PHRASES are removed.
        expected = ('The Pool Players.  .\n '
            ' real cool.  .  Strike straight.  g  . '
            ' Thin gin.  Jazz June.  Die soon.')
        eq_(expected, self.MockEpubFilter.filter(original))

        # Phrases are filtered in order.
        class TieredFilter(EpubFilter):
            FILTERED_PHRASES = [
                'Left school\.',
                'Real cool\. Left school\. Lurk late\.'
            ]

        result = TieredFilter.filter('Real cool. Left school. Lurk late.')
        eq_('Real cool.   Lurk late.', result)


class TestBibblioCoverageProvider(DatabaseTest):

    def setup(self):
        super(TestBibblioCoverageProvider, self).setup()

        self.base_url_setting = ConfigurationSetting.sitewide(
            self._db, Configuration.BASE_URL_KEY)
        self.base_url_setting.value = u'https://www.testing.code'

        self.edition, _lp = self._edition(
            with_open_access_download=True,
            data_source_name=BibblioCoverageProvider.INSTANT_CLASSICS_SOURCES[0]
        )
        self.identifier = self.edition.primary_identifier
        # Create a work for fiction/nonfiction status.
        self.work = self._work(presentation_edition=self.edition, fiction=False)

        self.custom_list, _ = self._customlist(
            foreign_identifier=u'fake-list', name=u'Fake List',
            data_source_name=DataSource.LIBRARY_STAFF, num_entries=0)
        # Add the edition to the list.
        self.custom_list.add_entry(self.edition)

        self.provider = BibblioCoverageProvider(
            self._db, self.custom_list.foreign_identifier, api=MockBibblioAPI()
        )

    def sample_file(self, filename):
        return sample_data(filename, 'bibblio')

    def add_representation(self, source_name, media_type, content,
                           identifier=None
    ):
        """Utility method to add representations to the local identifier"""
        identifier = identifier or self.identifier
        [pool] = identifier.licensed_through
        url = self._url + '/' + media_type
        source = DataSource.lookup(self._db, source_name)

        representation = None
        links = filter(lambda l: (
            l.rel==Hyperlink.OPEN_ACCESS_DOWNLOAD and
            l.resource.representation.media_type==media_type),
            identifier.links
        )
        if links:
            # There's already an open access link with the proper media
            # type. Just update its content.
            link = links[0]
            representation = link.resource.representation
            representation.content = content

            # And its data_sources.
            representation.data_source = source
            link.data_source = link.resource.data_source = source

        if not representation:
            link, _ = identifier.add_link(
                Hyperlink.OPEN_ACCESS_DOWNLOAD, url, source,
                media_type=media_type, content=content,
            )
            LicensePoolDeliveryMechanism.set(
                source, identifier, media_type, DeliveryMechanism.NO_DRM,
                RightsStatus.GENERIC_OPEN_ACCESS, resource=link.resource
            )
            representation = link.resource.representation

        return representation

    def test_items_that_need_coverage(self):
        # Use a random acceptable DataSource type.
        end = len(self.provider.INSTANT_CLASSICS_SOURCES)
        source_name = self.provider.INSTANT_CLASSICS_SOURCES[choice(range(0,end))]
        source = DataSource.lookup(self._db, source_name)

        # Create a nonfiction work that is not in the CustomList.
        listless = self._work(with_open_access_download=True, fiction=False)

        # Create a nonfiction work that's in the CustomList, even though
        # its Work isn't directly connected. (A CustomListEntry is not
        # always linked with a Work, though it always has an edition.)
        edition_listed = self._work(with_open_access_download=True, fiction=False)
        entry, _is_new = self.custom_list.add_entry(edition_listed.presentation_edition)
        entry.work = None

        # Create a work that already has coverage.
        covered = self._work(with_open_access_download=True, fiction=False)
        WorkCoverageRecord.add_for(covered, operation=self.provider.operation)

        # Create a fiction work without coverage.
        fiction = self._work(with_open_access_download=True)

        # And here's a nonfiction work without coverage.
        nonfiction = self.work

        for work in [covered, fiction, nonfiction]:
            self.custom_list.add_entry(work.presentation_edition)

        # Give all the works a target DataSource.
        works = [listless, edition_listed, covered, fiction]
        for work in works:
            work.presentation_edition.data_source = source
            for lp in work.license_pools:
                [delivery_mechanism] = lp.identifier.delivery_mechanisms
                lp.data_source = delivery_mechanism.data_source = source


        result = self.provider.items_that_need_coverage()
        # A Work that's not in the list is not included in the result.
        assert listless not in result
        # Unless its presentation_edition is in the list!
        assert edition_listed in result
        # A Work that's already covered is not included.
        assert covered not in result
        # By default, fiction is not included in the result.
        assert fiction not in result
        # A nonfiction Work that's not covered is included in the result.
        assert nonfiction in result


        # Unset Work.fiction for the work with its edition listed for
        # the next test.
        edition_listed.fiction = None

        # When fiction is being included, fiction and undefined
        # editions are included.
        self.provider.fiction = True
        result = self.provider.items_that_need_coverage()
        assert fiction in result
        assert edition_listed in result
        # But nonfiction is left behind.
        assert nonfiction not in result
        # And other ignored editions are ignored.
        assert listless not in result
        assert covered not in result

    def test_process_item(self):
        representation = self.identifier.links[0].resource.representation
        representation.content = self.sample_file('180.epub')

        result = self.provider.process_item(self.work)
        eq_(self.work, result)

        # An equivalent identifier has been created for the original identifier.
        [equivalency] = self.identifier.equivalencies
        eq_(1.0, equivalency.strength)

        bibblio_id = equivalency.output
        eq_(Identifier.BIBBLIO_CONTENT_ITEM_ID, bibblio_id.type)
        eq_('510b1ee0-bede-4e24-a379-6a387f2dbb64', bibblio_id.identifier)

        def assert_is_coverage_failure_for(result, item, data_source, transient=True):
            eq_(True, isinstance(result, CoverageFailure))
            eq_(item, result.obj)
            eq_(data_source, result.data_source)
            eq_(transient, result.transient)

        # When the API raises an error, a CoverageFailure is returned.
        self.provider.api.error = BadResponseException(
            'fake-bibblio.org', 'Got a bad 401')
        result = self.provider.process_item(self.work)

        assert_is_coverage_failure_for(
            result, self.work, self.provider.data_source
        )
        assert 'fake-bibblio.org' in result.exception
        assert '401' in result.exception

        # In fact, when any error is raised, an appropriate
        # CoverageFailure is returned.
        self.provider.api.error = ValueError("B A N A N A S")
        result = self.provider.process_item(self.work)
        assert_is_coverage_failure_for(
            result, self.work, self.provider.data_source
        )
        eq_("B A N A N A S", result.exception)

    def test_content_item_from_work(self):
        self.add_representation(
            DataSource.PLYMPTON, Representation.EPUB_MEDIA_TYPE,
            self.sample_file('180.epub')
        )
        result = self.provider.content_item_from_work(self.work)

        eq_(
            ['customUniqueIdentifier', 'name', 'provider', 'text', 'url'],
            sorted(result.keys())
        )
        eq_('%s by %s' % (self.edition.title, self.edition.author), result.get('name'))
        eq_({ 'name' : DataSource.PLYMPTON }, result['provider'])

        url_urn = quote(self.identifier.urn).replace('/', '%2F')
        expected_url = u'https://www.testing.code/lookup?urn=' + url_urn
        eq_(expected_url, result['url'])

    def test_edition_permalink(self):
        urn = quote(self.identifier.urn).replace('/', '%2F')
        result = self.provider.edition_permalink(self.edition)
        expected = 'https://www.testing.code/lookup?urn=%s' % urn
        eq_(expected, result)

    def test_get_full_text_uses_easiest_representation(self):
        epub_content = self.sample_file('180.epub')

        epub_rep = self.add_representation(
            DataSource.FEEDBOOKS, Representation.EPUB_MEDIA_TYPE, epub_content
        )

        text_rep = self.add_representation(
            DataSource.GUTENBERG, Representation.TEXT_PLAIN, 'blah'
        )

        text, data_source = self.provider.get_full_text(self.work)
        # The plaintext is used instead of the EPUB.
        eq_('blah', text)
        eq_(DataSource.GUTENBERG, data_source.name)

        # If a text representation doesn't have content, a different
        # text representation is selected.
        text_rep.content = None
        html_rep = self.add_representation(
            DataSource.UNGLUE_IT, Representation.TEXT_HTML_MEDIA_TYPE,
            '<p>BLEH</p>'
        )
        # In this case, the html representation is selected. Its HTML
        # tags have been removed.
        text, data_source = self.provider.get_full_text(self.work)
        eq_('BLEH', text)
        eq_(DataSource.UNGLUE_IT, data_source.name)

        # When text isn't readily available, content comes from the EPUB.
        html_rep.resource.representation = None
        text, data_source = self.provider.get_full_text(self.work)
        eq_(True, 'Dostoyevsky' in text)
        eq_(DataSource.FEEDBOOKS, data_source.name)

    def test_get_full_text_ignores_bad_representations(self):
        # If there's no representation to be found, nothing is returned.
        text, data_source = self.provider.get_full_text(self.work)
        eq_(None, text)
        eq_(None, data_source)

        # If there isn't a representation with a 200 or None status code,
        # nothing is returned.
        text_rep = self.add_representation(
            DataSource.GUTENBERG, Representation.TEXT_PLAIN, "Error"
        )
        text_rep.status_code = 403

        text, data_source = self.provider.get_full_text(self.work)
        eq_(None, text)
        eq_(None, data_source)

        # When the status code is corrected, there's a result.
        text_rep.status_code = None
        text, data_source = self.provider.get_full_text(self.work)
        eq_("Error", text)
        eq_(DataSource.GUTENBERG, data_source.name)

    def test_extract_plaintext_from_epub(self):
        source = DataSource.lookup(self._db, DataSource.FEEDBOOKS)
        epub = self.sample_file('180.epub')
        result = None

        with EpubAccessor.open_epub('677.epub', content=epub) as (zip_file, package_path):
            result = BibblioCoverageProvider.extract_plaintext_from_epub(
                zip_file, package_path, source
            )

        # We get back a string.
        eq_(True, isinstance(result, str))

        # To save space, it doesn't have any doubled whitespace.
        double_space = re.compile('\s\s')
        eq_(None, double_space.match(result))

        # Also, in accordance with the Bibblio API, it's not
        # over 200KB. Even though the book (The Brothers Karamazov)
        # is definitely longer.
        eq_(
            BibblioCoverageProvider.BIBBLIO_TEXT_LIMIT,
            len(result)
        )
