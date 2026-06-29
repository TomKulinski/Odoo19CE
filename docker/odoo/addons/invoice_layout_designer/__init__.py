from . import models
from . import controllers
from . import wizards


def post_init_seed_defaults(env):
    """Fresh installs: seed every default template that the XML left empty.

    The XML data file ships full element_ids for Modern Invoice + the three
    DIN 5008 sale templates. The remaining Modern templates (Quotation,
    Delivery, Purchase) intentionally ship as skeletons so the same Python
    seeder ( _create_modern_elements ) drives them — which keeps invoice,
    quotation, delivery and purchase in visual lock-step.
    """
    env["document.layout.template"]._seed_default_templates_if_empty()