#!/usr/bin/env python3
"""Regression checks for host CA-bundle discovery and environment precedence."""

from __future__ import annotations

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import tauceti_worker.paths as worker_paths

with tempfile.TemporaryDirectory(prefix="tauceti-certs-") as raw_tmp:
    tmp = Path(raw_tmp)
    nix_bundle = tmp / "nix.pem"
    python_bundle = tmp / "python.pem"
    common_bundle = tmp / "common.pem"
    for bundle in (nix_bundle, python_bundle, common_bundle):
        bundle.write_text("test CA bundle")

    defaults = SimpleNamespace(openssl_cafile=str(python_bundle))
    with (
        mock.patch("ssl.get_default_verify_paths", return_value=defaults),
        mock.patch.object(worker_paths, "_COMMON_CA_BUNDLES", (str(common_bundle),)),
    ):
        explicit = {"SSL_CERT_FILE": str(tmp / "operator-choice.pem")}
        assert worker_paths.ensure_ssl_cert_file(explicit) == explicit["SSL_CERT_FILE"]

        advertised = {"NIX_SSL_CERT_FILE": str(nix_bundle)}
        assert worker_paths.ensure_ssl_cert_file(advertised) == str(nix_bundle)
        assert advertised["SSL_CERT_FILE"] == str(nix_bundle)

        missing_nix = {"NIX_SSL_CERT_FILE": str(tmp / "missing.pem")}
        assert worker_paths.ensure_ssl_cert_file(missing_nix) == str(python_bundle)

        python_bundle.unlink()
        assert worker_paths.ensure_ssl_cert_file({}) == str(common_bundle)

        # An ambient SSL_CERT_FILE must not leak into discovery for an isolated mapping.
        saved_ssl_cert = os.environ.get("SSL_CERT_FILE")
        os.environ["SSL_CERT_FILE"] = str(nix_bundle)
        try:
            assert worker_paths.ensure_ssl_cert_file({}) == str(common_bundle)
        finally:
            if saved_ssl_cert is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = saved_ssl_cert

        common_bundle.unlink()
        empty: dict[str, str] = {}
        assert worker_paths.ensure_ssl_cert_file(empty) is None
        assert "SSL_CERT_FILE" not in empty

    with (
        mock.patch.object(worker_paths, "_ssl_cert_candidates", return_value=("~/ca.pem",)),
        mock.patch.object(Path, "expanduser", side_effect=RuntimeError("no home directory")),
    ):
        assert worker_paths.ensure_ssl_cert_file({}) is None

print("ssl cert file: OK")
