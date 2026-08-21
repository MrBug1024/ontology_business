# Ontology Platform Python SDK

`OntologyPlatformClient` targets the safe, read-only P2 external API boundary.
Create a scoped credential through `POST /api/developer/api-keys`; copy its
`token` immediately, because it is intentionally never returned again.

```python
from sdk import OntologyPlatformClient

with OntologyPlatformClient(
    "https://platform.example.com/api/external/v1",
    "ont_sk_...",
) as client:
    for scenario in client.list_scenarios():
        print(scenario["name"])
```

The client sends the credential only in the `X-API-Key` header.  v1 supports
`scenarios:read` and `objects:read`; platform RBAC, ACL and sensitive-property
filtering remain active for the key subject on every request.

HTTPS is required by default.  A local mock can opt in with
`allow_insecure_http=True`, but only for `localhost` or a numeric loopback
address; the SDK never follows HTTP redirects while an API key is attached.
