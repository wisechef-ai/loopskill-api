"""evergreen_0206 Phase D — CDN-fronted immutable tarball headers.

The _download route streams versioned skill tarballs. Tarballs are IMMUTABLE
per (skill, version) — a given version's bytes never change. So they can be
cached forever at the edge. Cloudflare already fronts origin (config.py:173).

This suite pins the cache contract so repeat pulls are served from Cloudflare's
edge and the weak origin disk is hit once-per-version-globally:
  - Cache-Control: public, max-age=31536000, immutable
  - ETag: "<checksum_sha256>"  (content address = perfect validator)

See docs/reconcile-contract.md + plan decision #18.
"""

from __future__ import annotations

from app.install_routes import _immutable_cache_headers


class TestImmutableCacheHeaders:
    def test_headers_include_immutable_cache_control(self):
        h = _immutable_cache_headers("a" * 64)
        assert h["Cache-Control"] == "public, max-age=31536000, immutable"

    def test_etag_is_quoted_checksum(self):
        h = _immutable_cache_headers("a" * 64)
        assert h["ETag"] == '"' + "a" * 64 + '"'

    def test_checksum_also_exposed_raw(self):
        """Keep the existing X-Checksum-SHA256 header for non-HTTP-cache consumers."""
        h = _immutable_cache_headers("b" * 64)
        assert h["X-Checksum-SHA256"] == "b" * 64

    def test_missing_checksum_no_etag_no_immutable(self):
        """Without a checksum we cannot content-address → do NOT mark immutable.

        A tarball whose checksum we don't know could be mutated; caching it
        forever would be unsafe. Fall back to no-store so correctness wins.
        """
        h = _immutable_cache_headers(None)
        assert "ETag" not in h
        assert "immutable" not in h.get("Cache-Control", "")
        assert h["Cache-Control"] == "no-store"

    def test_empty_checksum_treated_as_missing(self):
        h = _immutable_cache_headers("")
        assert "ETag" not in h
        assert h["Cache-Control"] == "no-store"
