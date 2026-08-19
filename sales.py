"""The salesman's side — doc/SALES.md.

Two things live here, and neither of them writes to a prospect:

1. **The watch list's owner.** A firm the salesman marks with *Vormerken*
   carries their address (`customer.owner`); everything below is keyed by
   it. Who that is comes from the environment, not from a login: the app
   has no accounts, the operator page is behind the edge's basic auth, and
   one address is the whole configuration today.

2. **Who is due today** (§4) and **the mail that says so** (§5) — added in
   the next step of the build order.

The customer's own mail (`delivering.py`) is not touched by anything here.
Two e-mails, two audiences: the report goes to a subscribed customer when
there is something to report; the "Heute schreiben" mail goes to the
salesman when a prospect is worth writing to.
"""

import os

# The owners, as `user=mail,user=mail` — the edge's basic-auth user mapped to
# an address. One entry today; the format exists so a second salesperson is a
# configuration change rather than a code change (doc/SALES.md 3).
OWNERS_ENV = 'TM_SALES_OWNERS'
DEFAULT_OWNER_ENV = 'TM_SALES_OWNER'


def owners():
    """{basic-auth user: address} from TM_SALES_OWNERS, possibly empty."""
    out = {}
    for part in (os.environ.get(OWNERS_ENV) or '').split(','):
        part = part.strip()
        if '=' in part:
            user, mail = part.split('=', 1)
            if user.strip() and mail.strip():
                out[user.strip()] = mail.strip()
    return out


def default_owner():
    """The address a Vormerken is filed under when the request cannot say
    who pressed it — which is every request today: the app sees only the
    edge's "this passed basic auth" header, never the user name. Explicit
    TM_SALES_OWNER wins; otherwise the single configured owner; otherwise
    None, and the row is simply unwatched (no mail, no crash)."""
    one = (os.environ.get(DEFAULT_OWNER_ENV) or '').strip()
    if one:
        return one
    known = list(owners().values())
    return known[0] if len(known) == 1 else None
