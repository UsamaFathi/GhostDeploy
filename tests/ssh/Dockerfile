FROM ubuntu:24.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server openssh-client python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd \
    && useradd -m -s /bin/sh ghostfixture
WORKDIR /fixture
COPY pyproject.toml README.md LICENSE ./
COPY ghostdeploy ./ghostdeploy
RUN python3 -m venv /opt/ghostdeploy && /opt/ghostdeploy/bin/python -m pip install --no-cache-dir .
COPY tests/ssh/check_forced_command.py ./
RUN touch /fixture/.disposable-ssh-image
ENTRYPOINT ["/opt/ghostdeploy/bin/python", "/fixture/check_forced_command.py"]
