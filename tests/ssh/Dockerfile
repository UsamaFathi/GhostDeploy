FROM python:3.12-slim-bookworm
RUN apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=15 update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=15 install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd \
    && useradd -m -s /bin/sh ghostfixture
WORKDIR /fixture
COPY pyproject.toml README.md LICENSE ./
COPY ghostdeploy ./ghostdeploy
RUN python -m venv /opt/ghostdeploy && /opt/ghostdeploy/bin/python -m pip install --no-cache-dir .
COPY tests/ssh/check_forced_command.py ./
RUN touch /fixture/.disposable-ssh-image
ENTRYPOINT ["/opt/ghostdeploy/bin/python", "/fixture/check_forced_command.py"]
