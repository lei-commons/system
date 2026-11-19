# Vendored DeDi schemas

`dedi-file.schema.json`, `dedi-manifest.schema.json`, `dedi-digests.schema.json` and `Public-Data-Set.json`
are copied unchanged from github.com/LF-Decentralized-Trust-labs/decentralized-directory-protocol
(`schemas/`, commit e63c71ea7da1e008ae660c7fd903f6d5f70d84ca, 2026-11-06; Apache-2.0). The protocol
still points their `$id`s at its mutable `main` branch; we validate against these pinned copies.
When the protocol tags a release, update them from the tag and rerun the conformance tests.
