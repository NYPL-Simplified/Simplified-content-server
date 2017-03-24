from nose.tools import set_trace
import contextlib
from core.config import (
    Configuration as CoreConfiguration,
    CannotLoadConfiguration,
    empty_config as core_empty_config,
    temp_config as core_temp_config,
)
from core.facets import FacetConstants as Facets

class Configuration(CoreConfiguration):

    S3_STATIC_FEED_BUCKET = "static_feed_bucket"

    BIBBLIO_INTEGRATION = "Bibblio"
    BIBBLIO_ID = "client_id"
    BIBBLIO_SECRET = "client_secret"

    DEFAULT_ENABLED_FACETS = {
        Facets.ORDER_FACET_GROUP_NAME : [
            Facets.ORDER_AUTHOR, Facets.ORDER_TITLE, Facets.ORDER_ADDED_TO_COLLECTION
        ],
        Facets.AVAILABILITY_FACET_GROUP_NAME : [
            Facets.AVAILABLE_OPEN_ACCESS
        ],
        Facets.COLLECTION_FACET_GROUP_NAME : [
            Facets.COLLECTION_FULL
        ]
    }

    DEFAULT_FACET = {
        Facets.ORDER_FACET_GROUP_NAME : Facets.ORDER_ADDED_TO_COLLECTION,
        Facets.AVAILABILITY_FACET_GROUP_NAME : Facets.AVAILABLE_OPEN_ACCESS,
        Facets.COLLECTION_FACET_GROUP_NAME : Facets.COLLECTION_FULL,
    }

@contextlib.contextmanager
def temp_config(new_config=None):
    with core_temp_config(new_config, [CoreConfiguration, Configuration]) as i:
        yield i
