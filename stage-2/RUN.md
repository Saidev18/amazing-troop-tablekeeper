# Tablekeeper Stage 2

From this directory, build and start with:

```sh
docker build -t tablekeeper-stage-2 . && docker run --rm -e PORT=8080 -p 8080:8080 tablekeeper-stage-2
```

The image contains Python and IANA timezone data and uses only the standard library. No runtime network access or external storage is required. State is in memory and reset on restart. A process-wide lock serializes state transactions, including checks and writes, idempotency receipts, reset, export and import. Passwords use salted scrypt; exported state contains hashes and session tokens and should be treated as private.
