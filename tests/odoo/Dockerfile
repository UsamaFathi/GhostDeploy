ARG ODOO_TEST_VERSION
FROM odoo:${ODOO_TEST_VERSION} AS executor
USER root
RUN apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=20 update && apt-get install -y --no-install-recommends git python3-venv && rm -rf /var/lib/apt/lists/*
WORKDIR /fixture
COPY pyproject.toml README.md LICENSE ./
COPY ghostdeploy ./ghostdeploy
RUN python3 -m venv /opt/ghostdeploy && /opt/ghostdeploy/bin/python -m pip install --no-cache-dir .
COPY tests/odoo/check_upgrade.py tests/odoo/fixture.py tests/odoo/browser_check.py ./
RUN touch /fixture/.disposable-odoo-image
USER odoo

FROM executor AS browser
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN /opt/ghostdeploy/bin/python -m pip install --no-cache-dir playwright==1.64.1 \
    && /opt/ghostdeploy/bin/python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*
USER odoo
