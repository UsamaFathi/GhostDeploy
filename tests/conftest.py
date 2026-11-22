import pytest


@pytest.fixture
def config_data():
    return {
        "schema_version": 1,
        "project": {"name": "coffee"},
        "odoo": {
            "version": "19.0",
            "service": "odoo19",
            "executable": "/opt/odoo/odoo-bin",
            "config_file": "/etc/odoo.conf",
            "addons_path": ["/opt/addons"],
        },
        "repository": {"checkout_path": "/opt/addons"},
        "deployment": {"environment": "staging", "databases": ["company_a", "company_b"]},
    }
