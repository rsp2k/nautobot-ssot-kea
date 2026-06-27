# Dev image: Nautobot 3.x + nautobot-ssot + nautobot-dhcp-models + this plugin.
#
# nautobot-dhcp-models is not on PyPI yet, so we install it from a wheel built
# from the sibling repo and dropped in development/wheels/ (see the Makefile
# `wheel` target). src/ is bind-mounted at runtime for hot-reload.
FROM ghcr.io/nautobot/nautobot:3.0-py3.12

USER 0

COPY development/wheels/*.whl /tmp/wheels/
RUN pip install --no-cache-dir nautobot-ssot==4.4.0 /tmp/wheels/*.whl

COPY pyproject.toml README.md /opt/plugin/
COPY src /opt/plugin/src
# Deps are already satisfied (base image + the two installs above); --no-deps
# avoids pip trying to resolve the unpublished nautobot-dhcp-models from PyPI.
RUN pip install --no-cache-dir --no-deps -e /opt/plugin

USER nautobot
