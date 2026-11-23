ARG ODOO_TEST_VERSION
FROM odoo:${ODOO_TEST_VERSION}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git python3-venv && rm -rf /var/lib/apt/lists/*
WORKDIR /fixture
COPY pyproject.toml README.md LICENSE ./
COPY ghostdeploy ./ghostdeploy
RUN python3 -m venv /opt/ghostdeploy && /opt/ghostdeploy/bin/python -m pip install --no-cache-dir .
COPY tests/odoo/check_upgrade.py tests/odoo/fixture.py ./
RUN touch /fixture/.disposable-odoo-image
USER odoo
