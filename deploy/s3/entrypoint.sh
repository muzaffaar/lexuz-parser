#!/bin/sh
# Bundled S3-compatible object store (SeaweedFS, Apache-2.0) for the raw source snapshots.
# Credentials and the bucket come from the environment; nothing secret is stored in Git or in the image.
set -eu
cat > /tmp/s3.json <<JSON
{"identities":[{"name":"yurist","credentials":[{"accessKey":"${S3_ACCESS_KEY}","secretKey":"${S3_SECRET_KEY}"}],
"actions":["Admin","Read","Write","List","Tagging"]}]}
JSON
exec weed mini -dir=/data -s3.config=/tmp/s3.json -bucket="${S3_BUCKET}"
