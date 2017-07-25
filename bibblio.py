import json
import logging
import os
import re
from datetime import datetime, timedelta
from nose.tools import set_trace
from urlparse import urlparse

from bs4 import BeautifulSoup
from flask import url_for

from sqlalchemy import or_
from sqlalchemy.orm import (
    aliased,
    eagerload,
)

from core.config import (
    CannotLoadConfiguration,
    Configuration,
)
from core.coverage import (
    CoverageFailure,
    WorkCoverageProvider,
    WorkCoverageRecord,
)
from core.model import (
    Credential,
    CustomList,
    CustomListEntry,
    ConfigurationSetting,
    DataSource,
    DeliveryMechanism,
    Edition,
    ExternalIntegration,
    Identifier,
    Library,
    LicensePool,
    LicensePoolDeliveryMechanism,
    Representation,
    Resource,
    Work,
)
from core.util.epub import EpubAccessor
from core.util.http import HTTP


class BibblioAPI(object):

    API_ENDPOINT = u'https://api.bibblio.org/v1/'
    CATALOGUES_ENDPOINT = API_ENDPOINT + u'catalogues/'
    CONTENT_ITEMS_ENDPOINT = API_ENDPOINT + u'content-items/'

    TOKEN_CONTENT_TYPE = u'application/x-www-form-urlencoded'

    log = logging.getLogger(__name__)

    @classmethod
    def from_config(cls, _db):
        integration = ExternalIntegration.lookup(
            _db, ExternalIntegration.BIBBLIO, ExternalIntegration.METADATA_GOAL
        )

        if not integration or not (integration.username and integration.password):
            raise CannotLoadConfiguration('Bibblio improperly configured')

        return cls(_db, integration.username, integration.password)

    @classmethod
    def set_timestamp(cls, resource, create=False):
        """Adds a timestamp to a resource (catalogue or content item)"""

        now = datetime.utcnow().isoformat() + 'Z'
        resource['dateModified'] = now
        if create:
            resource['dateCreated'] = now

        return resource

    def __init__(self, _db, client_id, client_secret):
        self._db = _db
        self.client_id = client_id
        self.client_secret = client_secret
        self._credential = None

    @property
    def source(self):
        return DataSource.lookup(self._db, DataSource.BIBBLIO)

    @property
    def token(self):
        if (self._credential and
            self._credential.expires > datetime.utcnow()):
            return self._credential.credential

        credential = Credential.lookup(
            self._db, self.source, None, None, self.refresh_credential
        )
        return credential.credential

    @property
    def default_headers(self):
        return {
            'Authorization': 'Bearer '+self.token,
            'Content-Type': 'application/json'
        }

    def refresh_credential(self, credential):
        url = self.API_ENDPOINT + 'token'
        headers = {'Content-Type': self.TOKEN_CONTENT_TYPE}
        client_details = dict(client_id=self.client_id, client_secret=self.client_secret)

        response = HTTP.post_with_timeout(url, client_details, headers=headers)
        data = response.json()

        credential.credential = data.get('access_token')
        expires_in = data.get('expires_in')
        credential.expires = datetime.utcnow() + timedelta(0, expires_in * 0.9)
        self._credential = credential

    def create_catalogue(self, name, description=None):
        catalogue = dict(name=name)
        if description:
            catalogue['description'] = description

        catalogue = json.dumps(catalogue)
        response = HTTP.post_with_timeout(
            self.CATALOGUES_ENDPOINT, catalogue,
            headers=self.default_headers,
            allowed_response_codes=[201]
        )

        catalogue = response.json()

        name = catalogue.get('name')
        catalogue_id = catalogue.get('catalogueId')
        self.log.info(
            "New catalogue '%s' created with ID: %s", name, catalogue_id)

        return catalogue

    def get_catalogue(self, name):
        response = HTTP.get_with_timeout(
            self.CATALOGUES_ENDPOINT, headers=self.default_headers
        )

        if response.status_code == 200:
            catalogues = response.json().get('results')
            catalogue = filter(lambda c: c.get('name') == name, catalogues)
            if catalogue:
                return catalogue[0]
            else:
                return None

    def create_content_item(self, content_item):
        content_item = self.set_timestamp(content_item, create=True)
        content_item = json.dumps(content_item)
        response = HTTP.post_with_timeout(
            self.CONTENT_ITEMS_ENDPOINT, content_item,
            headers=self.default_headers,
            allowed_response_codes=[201]
        )

        content_item = response.json()

        name = content_item.get('name')
        content_item_id = content_item.get('contentItemId')
        self.log.info(
            "New content item created for '%s': '%s'", name, content_item_id)

        return content_item

    def delete_content_item(self, identifier):
        content_item_id = identifier

        if isinstance(identifier, Identifier):
            if not identifier.type == Identifier.BIBBLIO_CONTENT_ITEM_ID:
                raise TypeError('Identifier is not a Bibblio Content Item')

            content_item_id = identifier.identifier
        delete_url = self.CONTENT_ITEMS_ENDPOINT + content_item_id
        response = HTTP.request_with_timeout(
            'DELETE', delete_url, headers=self.default_headers,
            allowed_response_codes=[200]
        )

        if not isinstance(identifier, basestring) and response.status_code == 200:
            self._db.delete(identifier)
            self.log.info("DELETED: Bibblio Content Item '%s'" % content_item_id)


class EpubFilter(object):

    """A base class for source-specific EPUB filtering. This class
    removes front matter and distributor-specific text that can impact
    recommendations created by the BibblioAPI.
    """

    FILLER_RE = '\s*'

    ## Values for subclass definition ##

    # SPINE_IDREFS lists idref values in the EPUB spine that can be
    # completely ignored, usually because they're chock full of text
    # specific to the distributor without any useful text to support
    # recommendations.
    SPINE_IDREFS = None

    # FILTERED_PHRASES lists strings that are directly related to the
    # distributor in decreasing order of specificity.
    FILTERED_PHRASES = None

    @classmethod
    def filter_spine_idrefs(cls, spine_idrefs):
        """Returns spine idrefs that have not been blacklisted"""
        return [s for s in spine_idrefs if s not in cls.SPINE_IDREFS]

    @classmethod
    def phrase_regex(cls, phrase):
        """Incorporates whitespace catchall string into a phrase"""
        length = len(phrase)
        if re.compile('\s{%d}' % length).match(phrase):
            # The phrase is all whitespace. Return it as is.
            return re.compile(phrase)
        words = [word for word in phrase.split() if word]
        phrase = cls.FILLER_RE.join(words)
        return re.compile(phrase, re.IGNORECASE)

    @classmethod
    def filter(cls, text):
        """Filters blacklisted phrases out of a text string"""
        filtered_text = text
        for phrase in cls.FILTERED_PHRASES:
            phrase_re = cls.phrase_regex(phrase)
            filtered_text = re.sub(phrase_re, ' ', filtered_text)
        return filtered_text


class GutenbergEpubFilter(EpubFilter):

    SPINE_IDREFS = set(['pg-header'])

    FILTERED_PHRASES = [
        (
            'This eBook is for the use of anyone anywhere (in the United'
            ' States)? (and)? (most other parts of the world)? at no'
            ' cost and with almost no restrictions whatsoever.'
        ),
        (
            'You may copy it, give it away or re-use it under the terms'
            ' of the Project Gutenberg License included with this eBook'
            ' or online at'
        ),
        (
            'If you are not located in the United States, you\'ll have'
            ' to check the laws of the country where you are located'
            ' before using this ebook.'
        ),
        '(http)?s?(:\/\/)?www\.gutenberg\.(org|net)(\/)?(\w|\.|-)*',
        '(The)? Project Gutenberg Ebook (of)?',
        '(The)? Project Gutenberg E(-)?text (of)?',
        'Project Gutenberg License',
        'Project Gutenberg',
        'Gutenberg',
    ]


class FeedbooksEpubFilter(EpubFilter):

    SPINE_IDREFS = set(['feedbooks', 'cover'])

    FILTERED_PHRASES = [
        'Note: This book is brought to you by Feedbooks',
        'Strictly for personal use, do not use this file for commercial purposes.',
        '(http)?s?(:\/\/)?www\.feedbooks\.com(\/)?',
        'FeedBooks',
    ]


class BibblioCoverageProvider(WorkCoverageProvider):

    SERVICE_NAME = u'Bibblio Coverage Provider'
    DEFAULT_BATCH_SIZE = 25
    OPERATION = u'bibblio-export'

    INSTANT_CLASSICS_SOURCES = [
        DataSource.FEEDBOOKS,
        DataSource.PLYMPTON,
        DataSource.STANDARD_EBOOKS,
    ]

    BIBBLIO_TEXT_LIMIT = 200000
    TEXT_MEDIA_TYPES = [
        Representation.TEXT_PLAIN,
        Representation.TEXT_HTML_MEDIA_TYPE,
    ]

    FILTERS_BY_DATASOURCE = {
        DataSource.GUTENBERG : GutenbergEpubFilter,
        DataSource.FEEDBOOKS : FeedbooksEpubFilter,
    }

    def __init__(self, _db, custom_list_identifier,
                 api=None, fiction=False, languages=None,
                 catalogue_identifier=None, **kwargs):
        super(BibblioCoverageProvider, self).__init__(_db, **kwargs)

        self.custom_list = CustomList.find(
            self._db, DataSource.LIBRARY_STAFF, custom_list_identifier
        )

        self.fiction = fiction
        self.languages = languages or []
        if not isinstance(self.languages, list):
            self.languages = [languages]

        self.api = api or BibblioAPI.from_config(self._db)
        self.catalogue_id = catalogue_identifier

    @property
    def data_source(self):
        return DataSource.lookup(self._db, DataSource.BIBBLIO)

    def items_that_need_coverage(self, identifiers=None, **kwargs):
        qu = super(BibblioCoverageProvider, self).items_that_need_coverage(
                identifiers=identifiers, **kwargs)

        data_sources = [DataSource.lookup(self._db, ds)
                        for ds in self.INSTANT_CLASSICS_SOURCES]

        # Get any identifiers with uncovered editions in the targeted
        # CustomList and an associated Work to be recommended.
        edition_entry = aliased(CustomListEntry)
        edition_list = aliased(CustomList)
        qu = qu.join(Work.presentation_edition)\
                .outerjoin(Work.custom_list_entries)\
                .outerjoin(CustomListEntry.customlist)\
                .outerjoin(edition_entry, Edition.custom_list_entries)\
                .outerjoin(edition_list, edition_entry.customlist)\
                .join(Work.license_pools)\
                .filter(
                    or_(
                        CustomList.id==self.custom_list.id,
                        edition_list.id==self.custom_list.id
                    )
                )\
                .options(eagerload(Work.presentation_edition)).distinct()

        library = Library.default(self._db)
        qu = library.restrict_to_ready_deliverable_works(qu, Work)
        qu = qu.filter(LicensePool.superceded==False)

        if not self.fiction:
            # Only get nonfiction. This is the default setting.
            qu = qu.filter(Work.fiction==False)
        else:
            qu = qu.filter(or_(Work.fiction==True, Work.fiction==None))

        if self.languages:
            # We only want a particular language.
            qu = qu.filter(Edition.language.in_(self.languages))

        return qu

    def process_item(self, work):
        try:
            content_item = self.content_item_from_work(work)
            result = self.api.create_content_item(content_item)
        except Exception as e:
            return CoverageFailure(
                work, str(e), data_source=self.data_source,
                transient=True
            )

        content_item_id = result.get('contentItemId')
        bibblio_identifier, _is_new = Identifier.for_foreign_id(
            self._db, Identifier.BIBBLIO_CONTENT_ITEM_ID, content_item_id
        )

        identifier = work.presentation_edition.primary_identifier
        identifier.equivalent_to(self.data_source, bibblio_identifier, 1)

        return work

    def content_item_from_work(self, work):
        edition = work.presentation_edition

        name = edition.title + ' by ' + edition.author
        url = self.edition_permalink(edition)
        text, data_source = self.get_full_text(work)

        if not text:
            raise ValueError(u'No text available for upload')

        data_source_name = data_source.name
        provider = dict(name=data_source_name)

        content_item = dict(
            name=name, url=url, text=text, provider=provider
        )

        custom_identifier = data_source.name+'|'+edition.primary_identifier.urn
        content_item['customUniqueIdentifier'] = custom_identifier

        if self.catalogue_id:
            content_item['catalogueId'] = self.catalogue_id

        return content_item

    def edition_permalink(self, edition):
        """Gets a unique URL for the target Work"""

        base_url = ConfigurationSetting.sitewide(self._db, Configuration.BASE_URL_KEY).value
        scheme, host = urlparse(base_url)[0:2]
        base_url = '://'.join([scheme, host])

        urn = edition.primary_identifier.urn
        initialization_value = os.environ.get('AUTOINITIALIZE')
        try:
            os.environ['AUTOINITIALIZE'] = 'False'
            from app import app
            with app.test_request_context(base_url=base_url):
                permalink = url_for('lookup', urn=urn, _external=True)

        finally:
            os.unsetenv('AUTOINITIALIZE')
            if initialization_value:
                os.environ['AUTOINITIALIZE'] = initialization_value

        return permalink

    def get_full_text(self, work):
        representations = self._db.query(Representation)\
            .join(Representation.resource)\
            .join(Resource.licensepooldeliverymechanisms)\
            .join(LicensePoolDeliveryMechanism.identifier)\
            .join(LicensePoolDeliveryMechanism.delivery_mechanism)\
            .join(Identifier.licensed_through)\
            .filter(
                LicensePool.work_id==work.id,
                DeliveryMechanism.drm_scheme==DeliveryMechanism.NO_DRM,
                or_(
                    Representation.status_code==200,
                    Representation.status_code==None
                )
            ).options(
                eagerload(Representation.resource, Resource.data_source))

        text_representation = representations.filter(
            Representation.media_type.in_(self.TEXT_MEDIA_TYPES),
            Representation.content.isnot(None))\
            .limit(1).all()

        if text_representation:
            # Get the full text if it's readily available.
            [representation] = text_representation
            data_source = representation.resource.data_source

            full_text = self._html_to_text(representation.content)
            full_text = self._shrink_text(full_text, data_source)
            return full_text, data_source

        # If it's gotta be an EPUB, make sure it matches the download url.
        epub_representations = representations.filter(
            Representation.media_type==Representation.EPUB_MEDIA_TYPE).all()

        if not epub_representations:
            # Access to the full text isn't available.
            return None, None

        for representation in epub_representations:
            url = representation.url
            content = representation.content
            data_source = representation.resource.data_source
            try:
                with EpubAccessor.open_epub(url, content=content) as (zip_file, package_path):
                    return (
                        self.extract_plaintext_from_epub(
                            zip_file, package_path, data_source
                        ),
                        data_source
                    )
            except Exception as e:
                continue

        # None of the epub Representations yielded text, and there's
        # nothing to be done about it. Carry on.
        return None, None

    @classmethod
    def extract_plaintext_from_epub(cls, zip_file, package_document_path, data_source):
        spine, manifest = EpubAccessor.get_elements_from_package(
            zip_file, package_document_path, ['spine', 'manifest']
        )

        # Get all of the items in the spine, where an ordered, TOC-esque
        # list of textual reading content is located by identifier.
        text_basefiles = list()
        for child in spine:
            if child.tag == '{%s}itemref' % EpubAccessor.IDPF_NAMESPACE:
                text_basefiles.append(child.get('idref'))

        # Get the elements that correspond to the textual reading content.
        epub_item_elements = list()
        for child in manifest:
            if (child.tag == '{%s}item' % EpubAccessor.IDPF_NAMESPACE
                and child.get('id') in text_basefiles):
                epub_item_elements.append(child)

        # Sort the items by their order in the spine.
        epub_item_elements.sort(key=lambda el: text_basefiles.index(el.get('id')))

        # Get the full EPUB filename for each text document.
        text_filenames = [el.get('href') for el in epub_item_elements]
        full_path = os.path.split(package_document_path)[0]
        text_filenames = [os.path.join(full_path, f) for f in text_filenames]

        accumulated_text = u''
        for filename in text_filenames:
            with zip_file.open(filename) as text_file:
                raw_text = cls._html_to_text(text_file.read())
                accumulated_text += (raw_text + '\n')

        return cls._shrink_text(accumulated_text, data_source)

    @classmethod
    def _shrink_text(cls, text, data_source, epub_filter_class=None):
        """Removes excessive whitespace and shortens text according to
        the API requirements
        """
        if not epub_filter_class:
            # Try to find an EpubFilterClass object for this DataSource
            if isinstance(data_source, DataSource):
                data_source = data_source.name
            epub_filter_class = cls.FILTERS_BY_DATASOURCE.get(data_source)

        if epub_filter_class:
            # Remove any unwanted text patterns that could impact
            # recommendations.
            text = epub_filter_class.filter(text)

        text = re.sub(r'(\s?\n\s+|\s+\n\s?)+', '\n', text)
        text = re.sub(r'\t{2,}', '\t', text)
        text = re.sub(r' {2,}', ' ', text)

        return text.encode('utf-8')[0:cls.BIBBLIO_TEXT_LIMIT]

    @classmethod
    def _html_to_text(cls, html_content):
        """Returns raw text from HTML"""
        return BeautifulSoup(html_content, 'lxml').get_text()
