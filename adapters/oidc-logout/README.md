# OIDC Back-Channel Logout verifier

Local, one-shot protocol adapter using `coreos/go-oidc` v3.21.0 `VerifyLogout`
and `go-jose` v4.1.4. Go 1.25 or newer is required to build.

```sh
go test ./...
go build -trimpath -o /opt/dios/bin/oidc-logout .
```

The DiOS receiver supplies a configured issuer, RP client ID, algorithm allowlist,
bounded trusted JWKS document, and token through stdin JSON. The adapter has no
network client. Successful stdout contains only verified event metadata;
invalid input exits 2 with a fixed error code. Tokens never appear in diagnostics.

Deployment profile: RS256/ES256, required `exp`, nonempty `jti`, `sid` or `sub`,
exact issuer and audience, no `nonce`, empty backchannel event object. `iat` must
be within the last 120 seconds or at most 30 seconds ahead; `exp` must follow
`iat` by at most 300 seconds. These time and algorithm limits are DiOS's supported
profile, narrower than the full OIDC specification. Older delayed messages need
the existing RFC 7662 reconciliation path. Authentication epoch comes only from
trusted `auth_time`, never from refreshed access-token `iat`.

Only public signing keys whose declared algorithm is allowed are eligible.
Input is capped at 128 KiB; tokens at 16 KiB; source identifiers at 160 bytes.
Pin and build the module with its checked-in `go.sum`. The backend has no Go
runtime dependency after the adapter is built. See
[receiver deployment and evidence](../../docs/design/backchannel-logout-35.md).
