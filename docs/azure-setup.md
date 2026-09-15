# Connecting AskAU to Azure

End to end: a Microsoft Entra tenant for identity, an Azure Blob Storage
container for documents, and the configuration that joins them to a running
AskAU.

Written while doing it. Every trap below is one that actually cost time, and
several of them fail silently — the system keeps working and quietly answers
nothing, which is worse than an error.

**You do not need any of this to run AskAU.** [local-setup.md](local-setup.md)
gets you a working system with seeded identities and no cloud account. Come here
when you want real sign-in and real documents.

---

## Part 1 — The tenant

**You probably already have one.** Signing into the Azure portal with a personal
Microsoft account provisions a workforce tenant called **Default Directory**,
with an initial domain derived from your address
(`<youraddress>.onmicrosoft.com`). Check under *Microsoft Entra ID* →
*Manage tenants*; the **Organization ID** column is your tenant id.

> **The Create button is gated.** *Manage tenants* → *Create* now reports
> "Customers must own a paid license to create Microsoft Entra Workforce tenant".
> That blocks nothing here: your existing Default Directory is a full workforce
> tenant, and everything below — users, groups, app registrations, group claims —
> is Entra ID Free. Only a *second* tenant needs a licence.

### Users

*Entra ID → Users → New user → Create new user.* Make three, because one identity
cannot demonstrate an access list:

| User | Purpose |
|---|---|
| `finance@<tenant>.onmicrosoft.com` | Sees confidential Finance material |
| `hr@<tenant>.onmicrosoft.com` | Sees confidential HR material |
| `newjoiner@<tenant>.onmicrosoft.com` | In no groups — the deny case |

Record each **Object ID**; you need it to provision their AskAU account.

Two things that will interrupt you:

**A forced password change on first sign-in.** If that first sign-in is the AskAU
redirect, you land on a password-change screen instead of back at the app, and it
reads like a broken callback. Sign each user in once in a private window first.

**Security defaults require MFA registration.** New tenants have them on, and the
"Let's keep your account secure" screen often has no skip. For a throwaway test
tenant: *Entra admin centre → Identity → Overview → Properties → Manage security
defaults → Disabled*. **Never on the AUC's real tenant** — and write down now that
production will have MFA enforced.

### Groups

*Entra ID → Groups → New group → type **Security***. Create `AskAU-All-Staff`,
`AskAU-Finance`, `AskAU-HR`.

**Then add the members.** This is easy to skip and fails invisibly: when a user
belongs to no groups, Entra omits the `groups` claim *entirely* rather than
sending an empty list, `DirectorySync` correctly declines to change anything, and
the reader ends up holding only their own principal. Every question returns "I
could not find enough evidence", and nothing anywhere says why. The login audit
row records `groups_claim_absent: true`, which is the fastest way to diagnose it.

Put `finance@` in All-Staff + Finance, `hr@` in All-Staff + HR, and leave
`newjoiner@` in none. Record each group's **Object ID**.

---

## Part 2 — App registrations

Three, and the separation is deliberate.

### `AskAU API` — defines the audience

*App registrations → New registration*, single tenant, no redirect URI.

- *Expose an API* → **Add** an Application ID URI → accept `api://<client-id>`
- *Add a scope*: `access_as_user`, admins and users, any consent text
- **Token configuration → Add groups claim → Security groups → Group ID**, for
  both ID and Access tokens

That last step is the one everyone misses. Entra emits no `groups` claim by
default; without it nobody gets any principals.

Record the **Application (client) ID** and the **Directory (tenant) ID**.

### `AskAU Web` — what the browser signs in with

*New registration*, single tenant, redirect URI **Web** →
`http://localhost:3000/api/auth/callback/microsoft-entra-id`.

- *Certificates & secrets* → **New client secret** → copy the **Value** now; it is
  shown once, and the Secret ID is not it
- *API permissions* → *My APIs* → `AskAU API` → `access_as_user` → **Grant admin
  consent**. Without consent, sign-in fails with `AADSTS65001`
- *Authentication* → add `http://localhost:3000/en/login` as a second Web redirect
  URI, so federated sign-out can return the user to the app

### `AskAU Ingestion` — reads documents with nobody signed in

*New registration*, single tenant, no redirect URI. Create a client secret.

Separate from the API's registration on purpose: this identity reads the whole
container unattended, the other validates user tokens, and one compromised secret
should not be both.

---

## Part 3 — Storage

*Storage accounts → Create.*

| Field | Value |
|---|---|
| Resource group | one of its own — you can delete the lot afterwards |
| Name | globally unique across Azure, not just your subscription |
| Region | near you; it is the download path for every document |
| Performance | Standard |
| Redundancy | **LRS** — geo-redundant costs roughly double and buys nothing here |

Leave the other tabs alone. The defaults you want are already set: anonymous blob
access disabled, secure transfer required, TLS 1.2.

Then *Data storage → Containers → + Container* → `policies`, access level
**Private**.

### The role assignment

*The container → Access Control (IAM) → Add role assignment →*
**Storage Blob Data Reader** *→ Members → + Select members.*

Three things go wrong here:

**Service principals do not appear in the default list.** Type the registration's
name in the search box. If nothing comes back, check the spelling of the app
registration itself.

**Assign on the container, not the account.** A misconfiguration is then bounded
by what you deliberately shared.

**Do not assign it to `AskAU-All-Staff` or the other groups.** They are right
there in the picker and look plausible, and they are the wrong kind of thing:
those groups are *document audiences* inside AskAU. Granting them Storage Blob
Data Reader would let every member read every blob directly through Azure,
bypassing per-document ACLs entirely — the precise thing the design exists to
prevent. One identity gets this role: the ingestion service principal. No humans.

> **Subscription Owner does not grant data-plane access.** You can create the
> storage account and not upload a blob to it — that needs *Storage Blob Data
> Contributor*, assigned separately. "I am Owner, why is this 403" is a confusing
> hour. Reading is all AskAU needs; you only hit this uploading test documents.

### What the blobs must carry

Blob storage has **no per-blob permissions** — access is granted at the container,
so every blob inside is equally reachable by whoever holds that grant. Ingest a
flat container under one grant and the product's central claim quietly becomes
false: every reader sees everything, with no error anywhere.

So each source *declares* where audiences come from, and there is no default. With
`acl_strategy: metadata`, each blob carries:

| Metadata key | Example | Meaning |
|---|---|---|
| `askau_principals` | `b0de0c6c-…` | **Required.** Comma-separated Entra object ids allowed to read it |
| `askau_classification` | `confidential` | `public` \| `internal` \| `confidential` \| `highly_restricted` |
| `askau_doc_type` | `policy` | Shown on every citation |
| `askau_department` | `Finance` | Shown on every citation |
| `askau_version` | `v2.1` | Shown on every citation |
| `askau_effective_from` | `2025-07-01` | `YYYY-MM-DD` |
| `askau_effective_to` | `2027-02-28` | Past dates leave default retrieval |
| `askau_family` | `travel-policy` | Optional: marks several blobs as revisions of one document |

A blob with no `askau_principals` is **not ingested** — it is reported as a run
failure with an actionable message. An unparseable date and an unknown
classification are refused the same way, rather than silently defaulted.

Uploading with metadata, using the CLI:

```bash
az storage blob upload --account-name <account> -c policies \
  -n "finance/relocation-grant.txt" -f ./relocation-grant.txt \
  --metadata askau_principals=<entra-group-oid> askau_classification=confidential \
             askau_doc_type=policy askau_department=Finance askau_version=v1.0
```

The two other strategies are `prefix` — a folder convention, `hr/` grants the HR
group, auditable at a glance and asking nothing of whoever uploads — and `fixed`,
one audience for the whole container.

---

## Part 4 — Configuration

**Backend** (`.env`) — see [configuration.md](configuration.md) for every variable:

```
ASKAU_AUTH_MODE=entra
ASKAU_ENTRA_TENANT_ID=<Directory (tenant) ID>
ASKAU_ENTRA_CLIENT_ID=<AskAU API client ID>
ASKAU_ENTRA_AUDIENCE=api://<AskAU API client ID>

ASKAU_AZURE_STORAGE_CLIENT_ID=<AskAU Ingestion client ID>
ASKAU_AZURE_STORAGE_CLIENT_SECRET=<its secret Value>
```

**Web client** (`askau-frontend/.env.local`):

```
NEXT_PUBLIC_USE_MOCK_API=false
ENTRA_TENANT_ID=<Directory (tenant) ID>
ENTRA_CLIENT_ID=<AskAU Web client ID>
ENTRA_CLIENT_SECRET=<the secret Value from AskAU Web>
ENTRA_API_SCOPE=api://<AskAU API client ID>/access_as_user
AUTH_SECRET=<openssl rand -base64 32>
NEXTAUTH_URL=http://localhost:3000
ASKAU_API_URL=http://127.0.0.1:8080
```

`AUTH_SECRET`, not only `NEXTAUTH_SECRET`: next-auth v5 reads the former, and with
only the v4 name set it silently generates an ephemeral secret — the session
evaporates seconds after a successful sign-in and the API proxy answers "Not
signed in" to a signed-in user.

`NEXT_PUBLIC_USE_MOCK_API=false` removes the dev bypass. That is the point, and it
also means a mistake anywhere above locks you out of the interface entirely — keep
a terminal handy for `curl`.

### Provision the AskAU accounts

A valid organisational token is not an AskAU account: onboarding assigns a
department and an access posture, and inventing those from a token would produce a
user whose authorization nobody decided. There is no endpoint. For each user,
using the **Object ID** from Part 1:

```bash
make provision-user OID=<entra-object-id> \
  EMAIL=finance@<tenant>.onmicrosoft.com NAME="Finance Tester" DEPARTMENT=Finance
```

Re-running is safe and additive; it never revokes a role. Add `DRY_RUN=1` to see
what it would do.

### Ingest

```bash
make ingest SOURCE=<knowledge_sources.id>
```

Register the source first, with `location` naming the account, container and ACL
strategy. The run reports what it discovered, indexed and refused, and exits
non-zero if anything failed.

---

## Part 5 — The corpus wrinkle

The seeded documents grant principals named `grp-hr`, `grp-finance`. Your Entra
groups have GUIDs. So a real user signs in, gets their GUID principals correctly,
matches no seeded document, and every question returns "I could not find enough
evidence". Nothing is broken — the two sides simply name groups differently.

**Do not rename the seeded principals.** It looks like the obvious fix and the
damage is delayed: dev tokens still claim the old names, so at the next dev
sign-in `DirectorySync` finds no principal by that name, mints an empty one, and
**removes** the renamed principal that held the grants. Measured: a seeded user
came out holding four principals, two of them new and empty, able to read nothing.

Add the Entra group as its own principal and mirror the grants instead, so both
naming schemes work at once — the SQL is in
`docs/architecture/15-decision-records.md` under ADR-0029. Then run
`make reconcile-acls`, which is needed here because this adds rows to
`document_acl`.

In a real deployment none of this arises: the connector writes Entra object ids
into `document_acl` from the start.

---

## Verifying it end to end

| Check | Expected |
|---|---|
| Sign in as `finance@` | Reaches the chat screen |
| `GET /api/v1/auth/me` | `principalCount` > 1 — self plus their groups. Exactly 1 means the groups claim did not arrive: revisit *Token configuration* |
| The login audit row | `groups_added` lists the group object ids |
| Ask a Finance question as `finance@` | Grounded, citing the confidential Finance document |
| Ask the **same** question as `hr@` | Refused, **zero** sources |
| Remove `finance@` from a group in Entra, sign in again | `principalCount` drops, `groups_removed` recorded, that material stops answering |
| `make ingest` against the container | Blobs with `askau_principals` indexed; one without it reported and refused |
| Sign out, then sign in | Microsoft asks which account — it does not silently resume |

The sixth row is the one to do carefully. A change made in the directory taking
effect in the product is the whole authorization story, and it is the only one
that cannot be faked with seed data.
