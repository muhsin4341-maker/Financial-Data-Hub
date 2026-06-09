#!/bin/bash
# LocalStack S3 initialisation — runs automatically on every container start.
# Creates buckets and sets CORS so the browser can PUT files directly.

set -e

echo "[localstack-init] Creating S3 buckets..."
awslocal s3 mb s3://fdh-documents-dev  --region us-east-1 2>/dev/null || true
awslocal s3 mb s3://fdh-exports-dev    --region us-east-1 2>/dev/null || true
awslocal s3 mb s3://fdh-assets-dev     --region us-east-1 2>/dev/null || true

echo "[localstack-init] Setting CORS on fdh-documents-dev..."
awslocal s3api put-bucket-cors \
  --bucket fdh-documents-dev \
  --cors-configuration '{
    "CORSRules": [{
      "AllowedOrigins": [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002"
      ],
      "AllowedMethods": ["PUT", "GET", "HEAD"],
      "AllowedHeaders": ["*"],
      "ExposeHeaders": ["ETag"],
      "MaxAgeSeconds": 3000
    }]
  }'

echo "[localstack-init] Done. Buckets ready."
awslocal s3 ls
