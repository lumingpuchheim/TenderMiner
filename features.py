"""Extract TED eForms notice XML into two Parquet tables, split by grain.

    python features.py --xml-dir data/raw/xml

  tenders.parquet  one row per (procedure, lot) at call time — ContractNotice only.
  awards.parquet   one row per LotResult, with the published tender details nested
                   underneath. In this corpus only the WINNING tender is published
                   (verified: every LotTender is referenced by a LotResult, and 99.6%
                   of multi-bid lots detail exactly one tender) — losing bids exist
                   only as counts in the submission statistics.

They are deliberately NOT one table. Every award column is post-outcome, so keeping
them apart means using an outcome as a feature requires writing a join rather than
forgetting to drop a column. The grains differ too: one award lot holds N LotTender
records, which no flat layout can hold alongside a single call-time row.

Join on (procedure_id, lot_id) to build a training table; dedupe corrigenda first by
keeping the highest notice_version per key.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import pyarrow as pa
import pyarrow.parquet as pq

# Without this, redirecting the run to a file buffers output in 8 KB blocks and a
# long parse looks identical to a hang.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)

NS = {
    'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
    'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'efac': 'http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1',
    'efbc': 'http://data.europa.eu/p27/eforms-ubl-extension-basic-components/1',
    'efext': 'http://data.europa.eu/p27/eforms-ubl-extensions/1',
}

# Lots carry schemeName="Lot"; "GLO" (lots group) and "Part" are different objects and
# must not become training rows.
LOT_SCHEMES = {'lot'}

# UN/CEFACT Rec.20 and the plain-English variants both appear in the wild.
UNIT_DAYS = {
    'DAY': 1, 'DAYS': 1, 'D': 1,
    'WEE': 7, 'WEEK': 7, 'WEEKS': 7, 'WK': 7,
    'MON': 30, 'MONTH': 30, 'MONTHS': 30, 'MO': 30,
    'ANN': 365, 'YEAR': 365, 'YEARS': 365, 'YR': 365,
}

# Selection criteria codes group by prefix along the three limbs of Dir. 2014/24 Art. 58.
# The family matters more than the raw count: three trade-register entries cost a bidder
# nothing, one "comparable reference works" requirement excludes firms outright.
CRITERION_FAMILY = {
    'slc-suit': 'suitability',   # Art. 58(2) — registration in a professional/trade register
    'slc-stand': 'financial',    # Art. 58(3) — turnover, ratios, insurance
    'slc-abil': 'technical',     # Art. 58(4) — references, staff, qualifications
}

# Codes appearing under lot-level ProcurementAdditionalType that describe the contract's
# nature rather than a strategic (green/social/innovation) objective.
NATURE_CODES = {'none', 'works', 'services', 'supplies'}

# eForms writes -1 where a value was deliberately withheld from publication (the reason
# is declared in FieldsPrivacy). Read as a number it becomes a contract worth minus one
# euro or a bid ranked -1, so it must become null.
WITHHELD = -1.0

# efbc:StatisticsCode values, flattened into named columns. Anything not listed here
# still survives in the raw `submission_statistics` list.
STATISTIC_COLUMNS = {
    'tenders': 'n_tenders',
    't-esubm': 'n_tenders_esubmission',
    't-sme': 'n_tenders_sme',
    't-micro': 'n_tenders_micro',
    't-small': 'n_tenders_small',
    't-med': 'n_tenders_medium',
    't-oth-eea': 'n_tenders_other_eea',
    't-no-eea': 'n_tenders_non_eea',
    't-verif-inad': 'n_tenders_inadmissible',
    't-verif-inad-low': 'n_tenders_abnormally_low',
    't-no-verif': 'n_tenders_not_verified',
    'part-req': 'n_participation_requests',
}


# --------------------------------------------------------------------------- helpers

# One event per NON-EMPTY value that a helper suppressed to null, keyed by
# (context, reason). Absent elements are never counted — absence is normal in eForms;
# a malformed present value is not, and silently nulling it hides sender-wide rot.
COERCIONS = collections.Counter()


def _record(context, reason):
    COERCIONS[(context, reason)] += 1


def _text(node, path):
    if node is None:
        return None
    el = node.find(path, NS)
    if el is None or el.text is None:
        return None
    val = el.text.strip()
    return val or None


def _texts(node, path):
    if node is None:
        return []
    out = []
    for el in node.findall(path, NS):
        if el is not None and el.text and el.text.strip():
            out.append(el.text.strip())
    return out


def _attr(node, path, name):
    if node is None:
        return None
    el = node.find(path, NS)
    return el.get(name) if el is not None else None


def _first(*values):
    """First non-empty value — used for lot-level field falling back to procedure-level."""
    for v in values:
        if v not in (None, '', []):
            return v
    return None


def _date(value, ctx='date'):
    """eForms dates look like '2026-03-24+01:00'."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value[:10])
    except ValueError:
        _record(ctx, 'bad_date')
        return None


def _float(value, ctx='float'):
    if value in (None, ''):
        return None
    try:
        return float(value)
    except ValueError:
        _record(ctx, 'bad_float')
        return None


def _bool(value, ctx='bool'):
    """eForms indicators are the strings 'true'/'false'. Anything else stays null."""
    if value is None:
        return None
    v = value.strip().lower()
    if v == 'true':
        return True
    if v == 'false':
        return False
    _record(ctx, f'bad_bool:{v[:20]}')
    return None


def _lfind(node, local_path):
    """Walk a '/'-separated path matching on local names, ignoring namespaces.

    The eForms extension blocks mix efac/efbc/cbc in ways that vary between senders and
    schema versions; matching on the local name avoids a wrong prefix guess silently
    producing an all-null column.
    """
    current = [node] if node is not None else []
    for step in local_path.split('/'):
        nxt = []
        for el in current:
            nxt.extend(c for c in el if c.tag.split('}')[-1] == step)
        current = nxt
        if not current:
            return []
    return current


def _ltext(node, local_path):
    for el in _lfind(node, local_path):
        if el.text and el.text.strip():
            return el.text.strip()
    return None


def _measure_days(node, path, ctx='measure'):
    """A DurationMeasure plus its @unitCode, normalised to days.

    Returns (days, raw_value, unit). An unrecognised unit yields days=None rather than
    an assumed day count — a raw '24' means 24 days or 24 years depending on the unit.
    """
    raw = _text(node, path)
    unit = (_attr(node, path, 'unitCode') or '').upper() or None
    if raw is None:
        return None, None, None
    try:
        n = int(float(raw))
    except (ValueError, OverflowError):  # OverflowError: int(float('1e400')) → inf
        _record(ctx, 'bad_measure')
        return None, _float(raw), unit
    if unit in UNIT_DAYS:
        days = n * UNIT_DAYS[unit]
    else:
        _record(ctx, f'unknown_unit:{unit}')
        days = None
    return days, float(n), unit


def _days_between(start, end):
    if not start or not end:
        return None
    return (end - start).days


def _number(value, ctx='number'):
    """A published numeric value, with the withheld sentinel mapped to null."""
    v = _float(value, ctx)
    if v == WITHHELD:
        _record(ctx, 'withheld')
        return None
    return v


def _count(value, ctx='count'):
    n = _number(value, ctx)
    return int(n) if n is not None else None


# ------------------------------------------------------------------- feature builders

def duration_days(project):
    """Contract duration in days, and where it came from.

    Two independent encodings exist in eForms and neither is reliably present:
      BT-36  PlannedPeriod/DurationMeasure + @unitCode  (unit is NOT always days)
      BT-536/BT-537  PlannedPeriod/StartDate + EndDate

    Prefer the explicit measure, fall back to the date span. The raw measure and unit
    are kept alongside so the 30-day-month / 365-day-year convention below can be
    revisited without re-parsing the XML.
    """
    days, raw, unit = _measure_days(project, 'cac:PlannedPeriod/cbc:DurationMeasure',
                                    'duration_measure')
    start = _date(_text(project, 'cac:PlannedPeriod/cbc:StartDate'), 'period_start')
    end = _date(_text(project, 'cac:PlannedPeriod/cbc:EndDate'), 'period_end')

    flags = []
    if start and end and start > end:
        flags.append('period_inverted')
    source = 'measure' if days is not None else None
    if days is None and start and end:
        span = (end - start).days + 1  # inclusive of both endpoints
        if span > 0:
            days = span
            source = 'dates'
    if days is not None and not (0 < days <= 365 * 20):
        days, source = None, None  # data-entry noise, not a 60-year contract
        flags.append('duration_out_of_range')
    return days, source, raw, unit, start, end, flags


def selection_criteria(lot):
    """Qualification requirements: how many, of what kind, and the raw pairs.

    Each efac:SelectionCriteria block is ONE requirement, so a lot yields a
    variable-length list. Count answers "how much burden", family/type answers "what
    kind" — they are different questions and neither substitutes for the other.

    The codes never carry the threshold (a code says "minimum turnover was required",
    not "EUR 2m"); that lives in the free-text description, kept here for later
    extraction. `slc-stand-other` is a catch-all and is the single most common code in
    this corpus, so a large share of the real burden is unparsed text.
    """
    pairs, families = [], collections.Counter()
    for crit in _lfind(lot, 'TenderingTerms/UBLExtensions/UBLExtension/ExtensionContent/'
                            'EformsExtension/SelectionCriteria'):
        code = _ltext(crit, 'TendererRequirementTypeCode')
        desc = _ltext(crit, 'Description')
        if code is None and desc is None:
            continue
        pairs.append({'type': code, 'description': desc})
        if code:
            family = next((f for p, f in CRITERION_FAMILY.items() if code.startswith(p)),
                          'other')
            families[family] += 1
    return pairs, families


def execution_requirements(terms):
    """Contract-execution conditions as (list_name, code, description) triples.

    The sub-lists einvoicing/ecatalog/esignature/nda/fsr/reserved-execution are clean
    enums. The 'conditions' list is NOT: its code is a category header ('performance')
    and the substance sits in 568 distinct free-text descriptions — kept verbatim here,
    deliberately unclassified (a later LLM pass, not a regex, is the right tool).
    """
    out = []
    if terms is None:
        return out
    for er in terms.findall('cac:ContractExecutionRequirement', NS):
        code_el = er.find('cbc:ExecutionRequirementCode', NS)
        code = code_el.text.strip() if code_el is not None and code_el.text else None
        desc = _text(er, 'cbc:Description')
        if code or desc:
            out.append({'list_name': code_el.get('listName') if code_el is not None else None,
                        'code': code, 'description': desc})
    return out


def exclusion_grounds(terms, root_terms):
    """Exclusion/participation codes, lot-level and procedure-level merged.

    The substantive grounds (exg-*) sit on the root TenderingTerms; lot terms carry the
    participation rules (late-*, res-ws, epo-*). A lot-only read would lose the exg-*
    codes entirely, so this is a union, deduped in first-seen order.
    """
    path = 'cac:TendererQualificationRequest/cac:SpecificTendererRequirement/cbc:TendererRequirementTypeCode'
    seen, out = set(), []
    for code in _texts(terms, path) + _texts(root_terms, path):
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out, sum(1 for c in out if c.startswith('exg-'))


def strategic_procurement(lot):
    """Green/social/innovation obligations as raw (code, value) pairs."""
    pairs = []
    for sp in _lfind(lot, 'TenderingTerms/UBLExtensions/UBLExtension/ExtensionContent/'
                          'EformsExtension/StrategicProcurement'):
        for el in sp.iter():
            if not list(el) and el.text and el.text.strip():
                pairs.append({'code': el.tag.split('}')[-1], 'value': el.text.strip()})
    return pairs


def organizations(root):
    """Map ORG-xxxx ids to buyer identity, so lots can be keyed to a real buyer.

    Buyer name/ID/address live once in the extension block; cac:ContractingParty only
    holds a pointer to the id.
    """
    orgs = {}
    for org in _lfind(root, 'UBLExtensions/UBLExtension/ExtensionContent/'
                            'EformsExtension/Organizations/Organization'):
        company = next(iter(_lfind(org, 'Company')), None)
        org_id = _ltext(company, 'PartyIdentification/ID')
        if not org_id:
            continue
        orgs[org_id] = {
            'buyer_name': _ltext(company, 'PartyName/Name'),
            'buyer_national_id': _ltext(company, 'PartyLegalEntity/CompanyID'),
            'buyer_city': _ltext(company, 'PostalAddress/CityName'),
            'buyer_postal_zone': _ltext(company, 'PostalAddress/PostalZone'),
            'buyer_nuts': _ltext(company, 'PostalAddress/CountrySubentityCode'),
            'buyer_country': _ltext(company, 'PostalAddress/Country/IdentificationCode'),
            'buyer_website': _ltext(company, 'WebsiteURI'),
            'buyer_is_cpb_awarding': _bool(_ltext(org, 'AwardingCPBIndicator')),
            'buyer_is_cpb_acquiring': _bool(_ltext(org, 'AcquiringCPBIndicator')),
            # Winner-side attributes; on award notices the winning firms live in the
            # same Organizations block as the buyer. Extra keys are ignored by the
            # tenders schema, so they ride along harmlessly for buyer orgs too.
            'company_size': _ltext(company, 'CompanySizeCode'),
            'n_ubos': len(_lfind(org, 'UltimateBeneficialOwner/ID')),
        }
    return orgs


EMPTY_BUYER = {k: None for k in (
    'buyer_name', 'buyer_national_id', 'buyer_city', 'buyer_postal_zone', 'buyer_nuts',
    'buyer_country', 'buyer_website', 'buyer_is_cpb_awarding', 'buyer_is_cpb_acquiring')}


def award_results(root, orgs, keys):
    """One row per LotResult — the outcome of a single lot.

    NoticeResult is a flat block of cross-referenced records rather than a tree:
    LotResult points at LotTender ids, each LotTender points at a TenderingParty id,
    each TenderingParty lists Tenderer org ids. They are indexed here and resolved so a
    row carries its winning tenders and winners inline. A LotResult references the
    winning tender(s), not every bid — when several tenders are referenced with ranks,
    winner_names keeps only the best rank, so a sender that ever references losing
    ranked tenders cannot silently inflate the winner list.
    """
    block = next(iter(_lfind(root, 'UBLExtensions/UBLExtension/ExtensionContent/'
                                   'EformsExtension/NoticeResult')), None)
    if block is None:
        return []

    # TenderingParty id -> the organisations bidding under it (a consortium has several)
    parties = {}
    for party in _lfind(block, 'TenderingParty'):
        pid = _ltext(party, 'ID')
        if pid:
            parties[pid] = [t for t in (_ltext(x, 'ID') for x in _lfind(party, 'Tenderer')) if t]

    # LotTender id -> one submitted bid
    bids = {}
    for bid in _lfind(block, 'LotTender'):
        bid_id = _ltext(bid, 'ID')
        if not bid_id:
            continue
        org_ids = parties.get(_ltext(bid, 'TenderingParty/ID'), [])
        for o in org_ids:
            if o not in orgs:
                _record('bid.tenderer', 'org_missing')
        bids[bid_id] = {
            'tender_id': bid_id,
            'tender_reference': _ltext(bid, 'TenderReference/ID'),
            'amount': _number(_ltext(bid, 'LegalMonetaryTotal/PayableAmount'), 'bid.amount'),
            'currency': next((e.get('currencyID') for e in
                              _lfind(bid, 'LegalMonetaryTotal/PayableAmount')), None),
            'rank': _count(_ltext(bid, 'RankCode')),
            'is_ranked': _bool(_ltext(bid, 'TenderRankedIndicator')),
            'is_variant': _bool(_ltext(bid, 'TenderVariantIndicator')),
            'subcontracting': _ltext(bid, 'SubcontractingTerm/TermCode'),
            'tenderer_ids': org_ids,
            'tenderer_names': [orgs[o]['buyer_name'] for o in org_ids
                               if o in orgs and orgs[o]['buyer_name']],
            'tenderer_sizes': [orgs[o]['company_size'] for o in org_ids if o in orgs],
            'withheld_fields': [c for c in (_ltext(f, 'FieldIdentifierCode')
                                            for f in _lfind(bid, 'FieldsPrivacy')) if c],
        }

    # SettledContract id -> the signed contract
    contracts = {}
    for contract in _lfind(block, 'SettledContract'):
        cid = _ltext(contract, 'ID')
        if cid:
            contracts[cid] = {
                'contract_id': cid,
                'contract_reference': _ltext(contract, 'ContractReference/ID'),
                'contract_title': _ltext(contract, 'Title'),
                'contract_signed_date': _date(_ltext(contract, 'IssueDate')),
                'contract_award_date': _date(_ltext(contract, 'AwardDate')),
            }

    notice_award_date = _date(_ltext(root, 'TenderResult/AwardDate'), 'award_date')
    notice_total = _number(_ltext(block, 'TotalAmount'), 'notice_total_amount')

    rows = []
    for result in _lfind(block, 'LotResult'):
        stats_raw, stats_named = [], {c: None for c in STATISTIC_COLUMNS.values()}
        for stat in _lfind(result, 'ReceivedSubmissionsStatistics'):
            code = _ltext(stat, 'StatisticsCode')
            value = _count(_ltext(stat, 'StatisticsNumeric'), 'submission_statistics')
            if code is None:
                continue
            stats_raw.append({'code': code, 'value': value})
            if code in STATISTIC_COLUMNS:
                stats_named[STATISTIC_COLUMNS[code]] = value

        bid_ids = [b for b in (_ltext(x, 'ID') for x in _lfind(result, 'LotTender')) if b]
        lot_bids = [bids[b] for b in bid_ids if b in bids]
        contract = next((contracts[c] for c in
                         (_ltext(x, 'ID') for x in _lfind(result, 'SettledContract'))
                         if c in contracts), {})
        # The LotResult reference is the winner signal: a sole referenced tender wins
        # even at rank 2-3 (the corpus has six such lots — the top-ranked bid was
        # excluded and the runner-up won). Rank only arbitrates when SEVERAL tenders
        # are referenced, so a sender listing losing ranked tenders cannot inflate
        # the winner list.
        ranked = [b['rank'] for b in lot_bids if b['rank'] is not None]
        if len(lot_bids) > 1 and ranked:
            best = min(ranked)
            winning = [b for b in lot_bids if b['rank'] is None or b['rank'] == best]
        else:
            winning = lot_bids
        winners = sorted({n for b in winning for n in b['tenderer_names']})
        winner_orgs = {o for b in winning for o in b['tenderer_ids'] if o in orgs}
        winner_sizes = {orgs[o]['company_size'] for o in winner_orgs} - {None}
        winner_size = winner_sizes.pop() if len(winners) == 1 and len(winner_sizes) == 1 else None
        n_ubos = sum(orgs[o]['n_ubos'] for o in winner_orgs)

        result_code = _ltext(result, 'TenderResultCode')
        lowest = _number(_ltext(result, 'LowerTenderAmount'), 'lowest_tender_amount')
        highest = _number(_ltext(result, 'HigherTenderAmount'), 'highest_tender_amount')
        n_tenders = stats_named['n_tenders']

        # Contradictions in the source are flagged, never fixed: the row still says
        # exactly what the notice said.
        quality = []
        if result_code == 'selec-w' and n_tenders == 0:
            quality.append('selected_but_zero_tenders')
        if winners and n_tenders == 0:
            quality.append('winner_but_zero_tenders')
        if lowest is not None and highest is not None:
            if lowest > highest:
                quality.append('tender_band_inverted')
            elif any(b['amount'] is not None and not (lowest <= b['amount'] <= highest)
                     for b in lot_bids):
                quality.append('winning_bid_outside_band')
        if len(lot_bids) > 1 and all(b['rank'] is None for b in lot_bids):
            quality.append('multiple_unranked_tenders')

        rows.append({
            **keys,
            'lot_result_id': _ltext(result, 'ID'),
            'lot_id': _ltext(result, 'TenderLot/ID'),
            'result_code': result_code,
            'decision_reason': _ltext(result, 'DecisionReason/DecisionReasonCode'),
            'award_date': notice_award_date,
            **stats_named,
            'submission_statistics': stats_raw,
            # Cheapest and dearest bid received — the spread a raw count cannot show.
            'lowest_tender_amount': lowest,
            'highest_tender_amount': highest,
            'n_winning_bids': len(lot_bids),
            'winning_bids': lot_bids,
            'winner_names': winners,
            'n_winners': len(winners),
            'winner_size': winner_size,
            'n_beneficial_owners': n_ubos,
            'contract_id': contract.get('contract_id'),
            'contract_reference': contract.get('contract_reference'),
            'contract_title': contract.get('contract_title'),
            'contract_signed_date': contract.get('contract_signed_date'),
            'contract_award_date': contract.get('contract_award_date'),
            'notice_total_amount': notice_total,
            'withheld_fields': [c for c in (_ltext(f, 'FieldIdentifierCode')
                                            for f in _lfind(block, 'FieldsPrivacy')) if c],
            'quality_flags': quality,
        })
    return rows


def document_references(terms):
    """Restricted flag, URL, and count over ALL CallForTendersDocumentReference blocks.

    A lot routinely carries several document references. Restricted means ANY of them
    is a restricted-document — judging only the first would export a lot with an open
    cover sheet and restricted tender documents as unrestricted. The URL prefers the
    first openly accessible reference.
    """
    refs = terms.findall('cac:CallForTendersDocumentReference', NS) if terms is not None else []
    restricted = [_text(ref, 'cbc:DocumentType') == 'restricted-document' for ref in refs]
    urls = [_text(ref, 'cac:Attachment/cac:ExternalReference/cbc:URI') for ref in refs]
    url = next((u for u, r in zip(urls, restricted) if u and not r), None) \
        or next((u for u in urls if u), None)
    return (any(restricted) if refs else None), url, len(refs)


# The criteria free text is platform boilerplate, not prose: ~145 code-less lots share
# ~25 distinct strings. Negation is its own rule and runs FIRST; price-only requires the
# whole (normalised) text to match an anchored known phrase — never a 'Preis' substring
# search, which the standard sentence 'Der Preis ist nicht das einzige
# Zuschlagskriterium' exists to punish. Anything unrecognised stays null and is counted,
# so new boilerplate variants surface in the run report instead of swelling the
# unknowns. The raw text is exported alongside for later (e.g. LLM) classification.
_MEAT_TEXTS = (
    'preis ist nicht das einzige zuschlagskriterium',
    'most economically advantageous tender',
)
_PRICE_ONLY_TEXTS = (
    re.compile(r'(der )?preis\.?$'),
    re.compile(r'(angebots|gesamt)preis$'),
    re.compile(r'(100 ?% ?preis|preis:? ?100 ?%)( preis)?\.?$'),
    re.compile(r'alleiniges zuschlagskriterium ?=? ?preis( alleiniges zuschlagskriterium ?=? ?preis)?$'),
    re.compile(r'niedrigster (angebots)?preis$'),
    re.compile(r'zusch(?:l)?agskriterium:? preis\b'),  # prefix: calculation detail may follow
)


def _classify_criteria_text(text):
    t = ' '.join(text.lower().split())
    if any(m in t for m in _MEAT_TEXTS):
        return 'meat'
    if any(p.match(t) for p in _PRICE_ONLY_TEXTS):
        return 'price-only'
    return None


def award_criteria(terms):
    """Criteria as published, a price-only / MEAT kind, and the raw fallback text.

    Weights sit in an extension *inside* each SubordinateAwardingCriterion, so they are
    paired per criterion rather than collected as a flat list.

    The kind comes from the TypeCodes when any exist. Without codes the corpus offers
    only free text — per-criterion Name/Description (7 lots) or, on lots that omit the
    criteria section entirely, Description/CalculationExpression one level up on
    AwardingCriterion (~145 lots). That text is classified by _classify_criteria_text
    and kept verbatim in award_terms_description / award_calculation_expression.

    Returns (criteria, kind, price_weight, terms_description, calculation_expression,
    kind_from_text).
    """
    criteria, price_weight = [], None
    terms_desc, calc_expr = None, None
    if terms is not None:
        parent = 'cac:AwardingTerms/cac:AwardingCriterion'
        terms_desc = '\n'.join(_texts(terms, parent + '/cbc:Description')) or None
        calc_expr = '\n'.join(_texts(terms, parent + '/cbc:CalculationExpression')) or None
        for crit in terms.findall(parent + '/cac:SubordinateAwardingCriterion', NS):
            ctype = _text(crit, 'cbc:AwardingCriterionTypeCode')
            pcode = _text(crit, './/efac:AwardCriterionParameter/efbc:ParameterCode')
            w = _float(_text(crit, './/efac:AwardCriterionParameter/efbc:ParameterNumeric'))
            criteria.append({
                'type': ctype,
                'name': _text(crit, 'cbc:Name'),
                'description': _text(crit, 'cbc:Description'),
                'weight': w,
                'weight_code': pcode,
            })
            # per-exa = exact percentage; other codes are points/orders, not comparable
            if w is not None and ctype == 'price' and pcode == 'per-exa':
                price_weight = w

    uniq = {c['type'] for c in criteria if c['type']}
    kind_from_text = False
    if uniq:
        kind = 'price-only' if uniq == {'price'} else 'meat'
    else:
        texts = [t for c in criteria for t in (c['name'], c['description']) if t]
        texts += [t for t in (terms_desc, calc_expr) if t]
        verdicts = {v for v in map(_classify_criteria_text, texts) if v}
        kind = verdicts.pop() if len(verdicts) == 1 else None
        kind_from_text = kind is not None
        if texts and kind is None:
            _record('award_criteria_text', 'unclassified')
    return criteria, kind, price_weight, terms_desc, calc_expr, kind_from_text


def parse_notice(path):
    root = ET.parse(path).getroot()
    kind = root.tag.split('}')[-1]

    notice_id = _text(root, 'cbc:ID')
    procedure_id = _text(root, 'cbc:ContractFolderID')
    issue_date = _date(_text(root, 'cbc:IssueDate'), 'issue_date')
    notice_subtype = _text(root, './/efext:EformsExtension/efac:NoticeSubType/cbc:SubTypeCode')
    notice_version = _text(root, 'cbc:VersionID')

    # --- procedure level: shared by every lot in the notice
    procedure_type = _text(root, 'cac:TenderingProcess/cbc:ProcedureCode')
    party = root.find('cac:ContractingParty', NS)
    # Two orthogonal buyer attributes. BT-11 is only required of contracting authorities
    # (Dir. 2014/24); sectoral entities (Dir. 2014/25) may carry the activity alone.
    buyer_legal_type = _text(party, 'cac:ContractingPartyType/cbc:PartyTypeCode')
    buyer_legal_type_list = _attr(party, 'cac:ContractingPartyType/cbc:PartyTypeCode', 'listName')
    buyer_activity = _text(party, 'cac:ContractingActivity/cbc:ActivityTypeCode')
    buyer_activity_list = _attr(party, 'cac:ContractingActivity/cbc:ActivityTypeCode', 'listName')

    root_project = root.find('cac:ProcurementProject', NS)
    root_process = root.find('cac:TenderingProcess', NS)
    root_terms = root.find('cac:TenderingTerms', NS)
    procedure_value = _number(_text(
        root_project, 'cac:RequestedTenderTotal/cbc:EstimatedOverallContractAmount'),
        'est_value_procedure')
    procedure_currency = _attr(
        root_project, 'cac:RequestedTenderTotal/cbc:EstimatedOverallContractAmount', 'currencyID')

    orgs = organizations(root)
    buyer = dict(EMPTY_BUYER)
    buyer_org_id = _text(party, 'cac:Party/cac:PartyIdentification/cbc:ID')
    if buyer_org_id:
        buyer.update(orgs.get(buyer_org_id, {}))
    buyer_profile_uri = _text(party, 'cbc:BuyerProfileURI')
    service_provider_type = _text(party, 'cac:Party/cac:ServiceProviderParty/cbc:ServiceTypeCode')

    # A corrigendum republishes the whole notice, so the same procedure appears several
    # times in a corpus. Dedupe on (procedure_id, lot_id) keeping the highest version.
    changed_notice_id = _ltext(root, 'UBLExtensions/UBLExtension/ExtensionContent/'
                                     'EformsExtension/Changes/ChangedNoticeIdentifier')
    change_reasons = [
        t for t in (_ltext(cr, 'ReasonCode') for cr in
                    _lfind(root, 'UBLExtensions/UBLExtension/ExtensionContent/'
                                 'EformsExtension/Changes/ChangeReason'))
        if t]

    # TED's own publication identity — machine-generated, 100% filled, and the public
    # join key to ted.europa.eu. PublicationDate can trail IssueDate by days; the
    # bidders' clock starts at publication.
    pub = 'UBLExtensions/UBLExtension/ExtensionContent/EformsExtension/Publication/'
    publication_number = _ltext(root, pub + 'NoticePublicationID')
    publication_date = _date(_ltext(root, pub + 'PublicationDate'), 'publication_date')
    gazette_id = _ltext(root, pub + 'GazetteID')

    keys = {
        'notice_id': notice_id,
        'notice_subtype': notice_subtype,
        'notice_version': notice_version,
        'procedure_id': procedure_id,
        'issue_date': issue_date,
        'source_file': os.path.basename(path),
        'publication_number': publication_number,
        'publication_date': publication_date,
        'gazette_id': gazette_id,
        'buyer_name': buyer['buyer_name'],
        'buyer_national_id': buyer['buyer_national_id'],
        'buyer_nuts': buyer['buyer_nuts'],
    }
    awards = award_results(root, orgs, keys) if kind == 'ContractAwardNotice' else []

    # Award notices repeat the scope (CPV, description, place) but drop every call-time
    # term, so their lots would be a differently-shaped row. They go to awards.parquet.
    if kind != 'ContractNotice':
        return [], awards

    lots = []
    for lot in root.findall('cac:ProcurementProjectLot', NS):
        lot_el = lot.find('cbc:ID', NS)
        scheme = (lot_el.get('schemeName') or '').lower() if lot_el is not None else ''
        if scheme not in LOT_SCHEMES:
            continue

        project = lot.find('cac:ProcurementProject', NS)
        process = lot.find('cac:TenderingProcess', NS)
        terms = lot.find('cac:TenderingTerms', NS)

        days, dur_source, dur_raw, dur_unit, start, end, dur_flags = duration_days(project)
        docs_restricted, docs_url, n_doc_refs = document_references(terms)
        (criteria_structs, crit_kind, price_weight,
         award_terms_desc, award_calc_expr, kind_from_text) = award_criteria(terms)
        criteria, families = selection_criteria(lot)
        exec_reqs = execution_requirements(terms)
        excl_codes, n_excl = exclusion_grounds(terms, root_terms)
        strategic = strategic_procurement(lot)
        validity_days, validity_raw, validity_unit = _measure_days(
            terms, 'cac:TenderValidityPeriod/cbc:DurationMeasure', 'bid_validity')

        bid_opening = _date(_text(process, 'cac:OpenTenderEvent/cbc:OccurrenceDate'),
                            'bid_opening_date')
        question_deadline = _date(_text(
            process, 'cac:AdditionalInformationRequestPeriod/cbc:EndDate'),
            'question_deadline_date')
        additional_types = [
            t for t in _texts(project, 'cac:ProcurementAdditionalType/cbc:ProcurementTypeCode')
            if t]

        deadline = _date(_first(
            _text(process, 'cac:TenderSubmissionDeadlinePeriod/cbc:EndDate'),
            _text(root_process, 'cac:TenderSubmissionDeadlinePeriod/cbc:EndDate')),
            'deadline_date')

        funding = _first(
            _texts(terms, 'cbc:FundingProgramCode'),
            _texts(root_terms, 'cbc:FundingProgramCode')) or []

        deadline_days = _days_between(issue_date, deadline)
        question_window = _days_between(issue_date, question_deadline)
        opening_lag = _days_between(deadline, bid_opening)
        quality = list(dur_flags)
        if kind_from_text:
            quality.append('criterion_kind_from_text')
        if deadline_days is not None and deadline_days < 0:
            quality.append('deadline_before_issue')
        if question_window is not None and question_window < 0:
            quality.append('question_deadline_before_issue')
        if opening_lag is not None and opening_lag < 0:
            quality.append('opening_before_deadline')

        lots.append({
            # --- keys
            'notice_id': notice_id,
            'notice_kind': kind,
            'notice_subtype': notice_subtype,
            'notice_version': notice_version,
            'procedure_id': procedure_id,
            'lot_id': _text(lot, 'cbc:ID'),
            'issue_date': issue_date,
            'source_file': os.path.basename(path),

            # 1. procedure type (BT-105)
            'procedure_type': procedure_type,
            'accelerated': _text(root_process, 'cac:ProcessJustification/cbc:ProcessReasonCode'),

            # 2. award criterion (BT-539 / BT-5421)
            'award_criterion_kind': crit_kind,
            'award_criteria': criteria_structs,
            'award_terms_description': award_terms_desc,
            'award_calculation_expression': award_calc_expr,
            'price_weight_pct': price_weight,

            # 3. lot structure (filled in once the notice's lots are all known)
            'n_lots': None,

            # 4. contract type (BT-23)
            'contract_type': _first(_text(project, 'cbc:ProcurementTypeCode'),
                                    _text(root_project, 'cbc:ProcurementTypeCode')),

            # 5. authority — two separate attributes, each with its own null rate
            'buyer_legal_type': buyer_legal_type,
            'buyer_legal_type_list': buyer_legal_type_list,
            'buyer_activity': buyer_activity,
            'buyer_activity_list': buyer_activity_list,

            # 6. funding source (BT-60)
            'eu_funded': ('eu-funds' in funding) if funding else None,
            'funding_programs': funding,

            # 7. contract duration — derived, see duration_days()
            'duration_days': days,
            'duration_source': dur_source,
            'duration_measure_raw': _float(dur_raw),
            'duration_unit_raw': dur_unit,
            'period_start': start,
            'period_end': end,

            # 8. CPV + free text
            'cpv_main': _first(
                _text(project, 'cac:MainCommodityClassification/cbc:ItemClassificationCode'),
                _text(root_project, 'cac:MainCommodityClassification/cbc:ItemClassificationCode')),
            'cpv_additional': _texts(
                project, 'cac:AdditionalCommodityClassification/cbc:ItemClassificationCode'),
            'title': _text(project, 'cbc:Name'),
            'description': _text(project, 'cbc:Description'),

            # 9. estimated value — lot and procedure scopes are NOT interchangeable
            'est_value_lot': _number(_text(
                project, 'cac:RequestedTenderTotal/cbc:EstimatedOverallContractAmount'),
                'est_value_lot'),
            'est_value_lot_currency': _attr(
                project, 'cac:RequestedTenderTotal/cbc:EstimatedOverallContractAmount', 'currencyID'),
            'est_value_procedure': procedure_value,
            'est_value_procedure_currency': procedure_currency,

            # 10. submission window
            'deadline_date': deadline,
            'deadline_days': deadline_days,
            'deadline_days_published': _days_between(publication_date, deadline),

            # ----- publication identity (TED-generated) -----
            'publication_number': publication_number,
            'publication_date': publication_date,
            'gazette_id': gazette_id,

            # ----- entry barriers -----
            # No criteria block at all is unknown, not zero: eForms lets a buyer omit
            # the section entirely, so 0 would assert something the notice never said.
            # Where criteria DO exist, a family count of 0 is a genuine zero.
            'n_selection_criteria': len(criteria) or None,
            'n_criteria_suitability': families['suitability'] if criteria else None,
            'n_criteria_financial': families['financial'] if criteria else None,
            'n_criteria_technical': families['technical'] if criteria else None,
            'n_criteria_other': families['other'] if criteria else None,
            'selection_criteria_types': sorted({c['type'] for c in criteria if c['type']}),
            'selection_criteria': criteria,
            'bid_bond_required': _bool(
                _text(terms, 'cac:RequiredFinancialGuarantee/cbc:GuaranteeTypeCode')),
            'bid_bond_description': _text(
                terms, 'cac:RequiredFinancialGuarantee/cbc:Description'),
            'bid_validity_days': validity_days,
            'bid_validity_raw': validity_raw,
            'bid_validity_unit': validity_unit,
            'execution_requirements': exec_reqs,
            'exclusion_grounds': excl_codes,
            'n_exclusion_grounds': n_excl,
            'cv_required': _text(terms, 'cbc:RequiredCurriculaCode'),
            'legal_form_required': _bool(
                _text(terms, 'cac:TendererQualificationRequest/cbc:CompanyLegalFormCode')),
            'security_clearance_required': _bool(_text(terms, 'cac:SecurityClearanceTerm/cbc:Code')),
            'docs_restricted': docs_restricted,
            'docs_url': docs_url,
            'n_doc_references': n_doc_refs,
            'variants': _text(terms, 'cbc:VariantConstraintCode'),
            'multiple_bids': _text(terms, 'cbc:MultipleTendersCode'),
            'esubmission': _text(process, 'cbc:SubmissionMethodCode'),

            # ----- scope / market -----
            'place_nuts3': _first(
                _text(project, 'cac:RealizedLocation/cac:Address/cbc:CountrySubentityCode'),
                _text(root_project, 'cac:RealizedLocation/cac:Address/cbc:CountrySubentityCode')),
            'place_city': _first(
                _text(project, 'cac:RealizedLocation/cac:Address/cbc:CityName'),
                _text(root_project, 'cac:RealizedLocation/cac:Address/cbc:CityName')),
            'place_postal_zone': _first(
                _text(project, 'cac:RealizedLocation/cac:Address/cbc:PostalZone'),
                _text(root_project, 'cac:RealizedLocation/cac:Address/cbc:PostalZone')),
            'sme_suitable': _bool(_text(project, 'cbc:SMESuitableIndicator')),
            'framework_type': _text(process, 'cac:ContractingSystem/cbc:ContractingSystemTypeCode'),
            'is_framework': (
                None if (ft := _text(process, 'cac:ContractingSystem/cbc:ContractingSystemTypeCode')) is None
                else ft != 'none'),
            'procurement_additional_types': additional_types,
            'is_strategic': (bool(set(additional_types) - NATURE_CODES)
                             if additional_types else None),
            'strategic_procurement': strategic,
            'procedure_languages': (_texts(terms, 'cac:Language/cbc:ID')
                                    or _texts(root_terms, 'cac:Language/cbc:ID')),
            'gpa_covered': _bool(_text(process, 'cbc:GovernmentAgreementConstraintIndicator')),
            'recurring': _bool(_text(terms, 'cbc:RecurringProcurementIndicator')),
            'eauction': _bool(_text(process, 'cac:AuctionTerms/cbc:AuctionConstraintIndicator')),

            # ----- timing beyond the deadline -----
            'bid_opening_date': bid_opening,
            'question_deadline_date': question_deadline,
            'question_window_days': question_window,
            'opening_lag_days': opening_lag,
            'is_corrigendum': changed_notice_id is not None,
            'changed_notice_id': changed_notice_id,
            'change_reasons': change_reasons,
            'n_corrections_so_far': None,  # filled by add_correction_counts()

            # ----- buyer & platform identity -----
            **buyer,
            'buyer_profile_uri': buyer_profile_uri,
            'service_provider_type': service_provider_type,
            'platform_url': _text(process, 'cbc:AccessToolsURI'),
            'platform_name': _ltext(process, 'UBLExtensions/UBLExtension/ExtensionContent/'
                                             'EformsExtension/AccessToolName'),
            'submission_endpoint': _text(terms, 'cac:TenderRecipientParty/cbc:EndpointID'),
            'quality_flags': quality,
        })

    n = len(lots)
    for row in lots:
        row['n_lots'] = n
    return lots, awards


# ----------------------------------------------------------------------------- schema

# ML role of every exported column, attached to the parquet schema as field metadata
# (survives the round trip; read via schema.field(name).metadata[b'role']). The role is
# what storage types cannot say: cpv_main and buyer_name are both strings, but one is a
# hierarchical code to truncate and the other an entity to aggregate, never one-hot.
#
#   key          identifier / join key — never a feature
#   categorical  finite code list (including list-of-codes columns), encode directly
#   hierarchical structured code — truncate to a prefix (CPV, NUTS, postal zone)
#   entity       named real-world party/place — feature only via aggregates
#   numeric / bool / date  use as-is
#   label        n_tenders — the training target, never a feature
#   text         free text — NLP/LLM territory
#   nested       list<struct> detail — explode or engineer before use
#   plumbing     URLs, endpoints, bookkeeping — not learnable
ROLES = {
    'notice_id': 'key', 'notice_kind': 'categorical', 'notice_subtype': 'categorical',
    'notice_version': 'plumbing', 'procedure_id': 'key', 'lot_id': 'key',
    'lot_result_id': 'key', 'issue_date': 'date', 'source_file': 'key',
    'publication_number': 'key', 'publication_date': 'date', 'gazette_id': 'plumbing',
    'procedure_type': 'categorical', 'accelerated': 'categorical',
    'award_criterion_kind': 'categorical', 'award_criteria': 'nested',
    'award_terms_description': 'text', 'award_calculation_expression': 'text',
    'price_weight_pct': 'numeric', 'n_lots': 'numeric', 'contract_type': 'categorical',
    'buyer_legal_type': 'categorical', 'buyer_legal_type_list': 'plumbing',
    'buyer_activity': 'categorical', 'buyer_activity_list': 'plumbing',
    'eu_funded': 'bool', 'funding_programs': 'categorical',
    'duration_days': 'numeric', 'duration_source': 'categorical',
    'duration_measure_raw': 'numeric', 'duration_unit_raw': 'categorical',
    'period_start': 'date', 'period_end': 'date',
    'cpv_main': 'hierarchical', 'cpv_additional': 'hierarchical',
    'title': 'text', 'description': 'text',
    'est_value_lot': 'numeric', 'est_value_lot_currency': 'categorical',
    'est_value_procedure': 'numeric', 'est_value_procedure_currency': 'categorical',
    'deadline_date': 'date', 'deadline_days': 'numeric',
    'deadline_days_published': 'numeric',
    'n_selection_criteria': 'numeric', 'n_criteria_suitability': 'numeric',
    'n_criteria_financial': 'numeric', 'n_criteria_technical': 'numeric',
    'n_criteria_other': 'numeric', 'selection_criteria_types': 'categorical',
    'selection_criteria': 'nested', 'bid_bond_required': 'bool',
    'bid_bond_description': 'text', 'bid_validity_days': 'numeric',
    'bid_validity_raw': 'numeric', 'bid_validity_unit': 'categorical',
    'execution_requirements': 'nested', 'exclusion_grounds': 'categorical',
    'n_exclusion_grounds': 'numeric', 'cv_required': 'categorical',
    'legal_form_required': 'bool', 'security_clearance_required': 'bool',
    'docs_restricted': 'bool', 'docs_url': 'plumbing', 'n_doc_references': 'numeric',
    'variants': 'categorical', 'multiple_bids': 'categorical',
    'esubmission': 'categorical',
    'place_nuts3': 'hierarchical', 'place_city': 'entity',
    'place_postal_zone': 'hierarchical', 'sme_suitable': 'bool',
    'framework_type': 'categorical', 'is_framework': 'bool',
    'procurement_additional_types': 'categorical', 'is_strategic': 'bool',
    'strategic_procurement': 'nested', 'procedure_languages': 'categorical',
    'gpa_covered': 'bool', 'recurring': 'bool', 'eauction': 'bool',
    'bid_opening_date': 'date', 'question_deadline_date': 'date',
    'question_window_days': 'numeric', 'opening_lag_days': 'numeric',
    'is_corrigendum': 'bool', 'changed_notice_id': 'key',
    'change_reasons': 'categorical', 'n_corrections_so_far': 'numeric',
    'buyer_name': 'entity', 'buyer_national_id': 'entity', 'buyer_city': 'entity',
    'buyer_postal_zone': 'hierarchical', 'buyer_nuts': 'hierarchical',
    'buyer_country': 'categorical', 'buyer_website': 'plumbing',
    'buyer_is_cpb_awarding': 'bool', 'buyer_is_cpb_acquiring': 'bool',
    'buyer_profile_uri': 'plumbing', 'service_provider_type': 'categorical',
    'platform_url': 'plumbing', 'platform_name': 'categorical',
    'submission_endpoint': 'plumbing', 'quality_flags': 'categorical',
    'result_code': 'categorical', 'decision_reason': 'categorical',
    'award_date': 'date', 'n_tenders': 'label',
    'n_tenders_esubmission': 'numeric', 'n_tenders_sme': 'numeric',
    'n_tenders_micro': 'numeric', 'n_tenders_small': 'numeric',
    'n_tenders_medium': 'numeric', 'n_tenders_other_eea': 'numeric',
    'n_tenders_non_eea': 'numeric', 'n_tenders_inadmissible': 'numeric',
    'n_tenders_abnormally_low': 'numeric', 'n_tenders_not_verified': 'numeric',
    'n_participation_requests': 'numeric', 'submission_statistics': 'nested',
    'lowest_tender_amount': 'numeric', 'highest_tender_amount': 'numeric',
    'n_winning_bids': 'numeric', 'winning_bids': 'nested',
    'winner_names': 'entity', 'n_winners': 'numeric', 'winner_size': 'categorical',
    'n_beneficial_owners': 'numeric', 'contract_id': 'key',
    'contract_reference': 'key', 'contract_title': 'text',
    'contract_signed_date': 'date', 'contract_award_date': 'date',
    'notice_total_amount': 'numeric', 'withheld_fields': 'categorical',
}


def _with_roles(schema):
    """Stamp each field's ML role into its metadata; KeyError = undeclared new field."""
    return pa.schema([f.with_metadata({'role': ROLES[f.name]}) for f in schema])


SCHEMA = pa.schema([
    ('notice_id', pa.string()),
    ('notice_kind', pa.string()),
    ('notice_subtype', pa.string()),
    ('notice_version', pa.string()),
    ('procedure_id', pa.string()),
    ('lot_id', pa.string()),
    ('issue_date', pa.date32()),
    ('source_file', pa.string()),
    ('procedure_type', pa.string()),
    ('accelerated', pa.string()),
    ('award_criterion_kind', pa.string()),
    ('award_criteria', pa.list_(pa.struct([
        ('type', pa.string()), ('name', pa.string()), ('description', pa.string()),
        ('weight', pa.float64()), ('weight_code', pa.string())]))),
    ('award_terms_description', pa.string()),
    ('award_calculation_expression', pa.string()),
    ('price_weight_pct', pa.float64()),
    ('n_lots', pa.int32()),
    ('contract_type', pa.string()),
    ('buyer_legal_type', pa.string()),
    ('buyer_legal_type_list', pa.string()),
    ('buyer_activity', pa.string()),
    ('buyer_activity_list', pa.string()),
    ('eu_funded', pa.bool_()),
    ('funding_programs', pa.list_(pa.string())),
    ('duration_days', pa.int32()),
    ('duration_source', pa.string()),
    ('duration_measure_raw', pa.float64()),
    ('duration_unit_raw', pa.string()),
    ('period_start', pa.date32()),
    ('period_end', pa.date32()),
    ('cpv_main', pa.string()),
    ('cpv_additional', pa.list_(pa.string())),
    ('title', pa.string()),
    ('description', pa.string()),
    ('est_value_lot', pa.float64()),
    ('est_value_lot_currency', pa.string()),
    ('est_value_procedure', pa.float64()),
    ('est_value_procedure_currency', pa.string()),
    ('deadline_date', pa.date32()),
    ('deadline_days', pa.int32()),
    ('deadline_days_published', pa.int32()),
    ('publication_number', pa.string()),
    ('publication_date', pa.date32()),
    ('gazette_id', pa.string()),

    ('n_selection_criteria', pa.int32()),
    ('n_criteria_suitability', pa.int32()),
    ('n_criteria_financial', pa.int32()),
    ('n_criteria_technical', pa.int32()),
    ('n_criteria_other', pa.int32()),
    ('selection_criteria_types', pa.list_(pa.string())),
    ('selection_criteria', pa.list_(pa.struct([
        ('type', pa.string()), ('description', pa.string())]))),
    ('bid_bond_required', pa.bool_()),
    ('bid_bond_description', pa.string()),
    ('bid_validity_days', pa.int32()),
    ('bid_validity_raw', pa.float64()),
    ('bid_validity_unit', pa.string()),
    ('execution_requirements', pa.list_(pa.struct([
        ('list_name', pa.string()), ('code', pa.string()), ('description', pa.string())]))),
    ('exclusion_grounds', pa.list_(pa.string())),
    ('n_exclusion_grounds', pa.int32()),
    ('cv_required', pa.string()),
    ('legal_form_required', pa.bool_()),
    ('security_clearance_required', pa.bool_()),
    ('docs_restricted', pa.bool_()),
    ('docs_url', pa.string()),
    ('n_doc_references', pa.int32()),
    ('variants', pa.string()),
    ('multiple_bids', pa.string()),
    ('esubmission', pa.string()),

    ('place_nuts3', pa.string()),
    ('place_city', pa.string()),
    ('place_postal_zone', pa.string()),
    ('sme_suitable', pa.bool_()),
    ('framework_type', pa.string()),
    ('is_framework', pa.bool_()),
    ('procurement_additional_types', pa.list_(pa.string())),
    ('is_strategic', pa.bool_()),
    ('strategic_procurement', pa.list_(pa.struct([
        ('code', pa.string()), ('value', pa.string())]))),
    ('procedure_languages', pa.list_(pa.string())),
    ('gpa_covered', pa.bool_()),
    ('recurring', pa.bool_()),
    ('eauction', pa.bool_()),

    ('bid_opening_date', pa.date32()),
    ('question_deadline_date', pa.date32()),
    ('question_window_days', pa.int32()),
    ('opening_lag_days', pa.int32()),
    ('is_corrigendum', pa.bool_()),
    ('changed_notice_id', pa.string()),
    ('change_reasons', pa.list_(pa.string())),
    ('n_corrections_so_far', pa.int32()),

    ('buyer_name', pa.string()),
    ('buyer_national_id', pa.string()),
    ('buyer_city', pa.string()),
    ('buyer_postal_zone', pa.string()),
    ('buyer_nuts', pa.string()),
    ('buyer_country', pa.string()),
    ('buyer_website', pa.string()),
    ('buyer_is_cpb_awarding', pa.bool_()),
    ('buyer_is_cpb_acquiring', pa.bool_()),
    ('buyer_profile_uri', pa.string()),
    ('service_provider_type', pa.string()),
    ('platform_url', pa.string()),
    ('platform_name', pa.string()),
    ('submission_endpoint', pa.string()),
    ('quality_flags', pa.list_(pa.string())),
])
SCHEMA = _with_roles(SCHEMA)

BID = pa.struct([
    ('tender_id', pa.string()),
    ('tender_reference', pa.string()),
    ('amount', pa.float64()),
    ('currency', pa.string()),
    ('rank', pa.int32()),
    ('is_ranked', pa.bool_()),
    ('is_variant', pa.bool_()),
    ('subcontracting', pa.string()),
    ('tenderer_ids', pa.list_(pa.string())),
    ('tenderer_names', pa.list_(pa.string())),
    ('tenderer_sizes', pa.list_(pa.string())),
    ('withheld_fields', pa.list_(pa.string())),
])

AWARD_SCHEMA = pa.schema([
    ('notice_id', pa.string()),
    ('notice_subtype', pa.string()),
    ('notice_version', pa.string()),
    ('procedure_id', pa.string()),
    ('lot_id', pa.string()),
    ('lot_result_id', pa.string()),
    ('issue_date', pa.date32()),
    ('source_file', pa.string()),
    ('publication_number', pa.string()),
    ('publication_date', pa.date32()),
    ('gazette_id', pa.string()),
    ('buyer_name', pa.string()),
    ('buyer_national_id', pa.string()),
    ('buyer_nuts', pa.string()),

    ('result_code', pa.string()),
    ('decision_reason', pa.string()),
    ('award_date', pa.date32()),

    ('n_tenders', pa.int32()),
    ('n_tenders_esubmission', pa.int32()),
    ('n_tenders_sme', pa.int32()),
    ('n_tenders_micro', pa.int32()),
    ('n_tenders_small', pa.int32()),
    ('n_tenders_medium', pa.int32()),
    ('n_tenders_other_eea', pa.int32()),
    ('n_tenders_non_eea', pa.int32()),
    ('n_tenders_inadmissible', pa.int32()),
    ('n_tenders_abnormally_low', pa.int32()),
    ('n_tenders_not_verified', pa.int32()),
    ('n_participation_requests', pa.int32()),
    ('submission_statistics', pa.list_(pa.struct([
        ('code', pa.string()), ('value', pa.int32())]))),

    ('lowest_tender_amount', pa.float64()),
    ('highest_tender_amount', pa.float64()),
    ('n_winning_bids', pa.int32()),
    ('winning_bids', pa.list_(BID)),
    ('winner_names', pa.list_(pa.string())),
    ('n_winners', pa.int32()),
    ('winner_size', pa.string()),
    ('n_beneficial_owners', pa.int32()),

    ('contract_id', pa.string()),
    ('contract_reference', pa.string()),
    ('contract_title', pa.string()),
    ('contract_signed_date', pa.date32()),
    ('contract_award_date', pa.date32()),
    ('notice_total_amount', pa.float64()),
    ('withheld_fields', pa.list_(pa.string())),
    ('quality_flags', pa.list_(pa.string())),
])
AWARD_SCHEMA = _with_roles(AWARD_SCHEMA)


def add_correction_counts(rows):
    """Corrections issued for this lot up to and including this notice.

    Computed across the whole chain, so it must run before any deduplication.

    This is a point-in-time count, not a total. The total is unknowable when a notice
    goes out — nobody knows yet how many corrections will follow — so using it as a
    call-time feature would leak the future. Counting only revisions at or before the
    current one keeps the row honest: it says what was known the day it published.

    Counts the is_corrigendum flag rather than the row's position in the chain. If the
    original call fell outside the download window the count still undercounts, but a
    row that declares itself a correction never reports zero.
    """
    chains = collections.defaultdict(list)
    for row in rows:
        key = (row.get('procedure_id'), row.get('lot_id'))
        chains[key if None not in key else ('', id(row))].append(row)
    for chain in chains.values():
        chain.sort(key=lambda r: (r.get('issue_date') or datetime.date.min,
                                  r.get('notice_id') or ''))
        seen = 0
        for row in chain:
            if row.get('is_corrigendum'):
                seen += 1
            row['n_corrections_so_far'] = seen
    return rows


def deduplicate(rows):
    """Collapse corrigenda: keep one row per (procedure_id, lot_id).

    A corrigendum republishes the whole notice, so the same lot appears once per
    revision — 1,076 of 3,415 tender rows in the current corpus. Left in, a lot that
    was corrected four times votes five times in training.

    The survivor is the latest issue_date, tie-broken by notice_version then notice_id.
    Note that cbc:VersionID is NOT a revision counter here — 98% of rows carry '01' and
    a lot corrected four times keeps '01' throughout — so ordering by it first would
    override the dispatch date, which is what actually tracks the chain.

    Keeping the latest revision also means the row carries the deadline bidders really
    faced: corrigenda in this corpus extend it, sometimes by months.

    Rows missing a key are kept as-is: they cannot be matched to anything, so dropping
    them would lose data rather than a duplicate.
    """
    best = {}
    unkeyed = []
    for row in rows:
        key = (row.get('procedure_id'), row.get('lot_id'))
        if key[0] is None or key[1] is None:
            unkeyed.append(row)
            continue
        try:
            version = int(row.get('notice_version') or 0)
        except ValueError:
            version = 0
        rank = (row.get('issue_date') or datetime.date.min, version,
                row.get('notice_id') or '')
        if key not in best or rank > best[key][0]:
            best[key] = (rank, row)
    return [row for _, row in best.values()] + unkeyed


def write_fields_doc(path):
    """FIELDS.md, generated from the schemas so it cannot drift from the code."""
    legend = {
        'key': 'identifier / join key — never a feature',
        'categorical': 'finite code list (incl. list-of-codes) — encode directly',
        'hierarchical': 'structured code — truncate to a prefix (CPV, NUTS, postal)',
        'entity': 'named party/place — feature only via aggregates, never one-hot',
        'numeric': 'number, use as-is', 'bool': 'boolean, use as-is',
        'date': 'date, use as-is (or derive spans)',
        'label': 'the training target — never a feature',
        'text': 'free text — NLP/LLM territory, kept verbatim',
        'nested': 'list<struct> detail — explode or engineer before use',
        'plumbing': 'URLs, endpoints, bookkeeping — not learnable',
    }
    lines = ['# Field dictionary', '',
             'Generated by `python features.py --fields-doc` — do not edit by hand.',
             'The same `role` tag is embedded in each parquet column\'s metadata:',
             '`pq.read_schema(path).field(name).metadata[b"role"]`.', '', '## Roles', '']
    lines += [f'- **{k}** — {v}' for k, v in legend.items()]
    for title, schema in (('tenders.parquet', SCHEMA), ('awards.parquet', AWARD_SCHEMA)):
        lines += ['', f'## {title}', '', '| column | type | role |', '| --- | --- | --- |']
        lines += [f'| `{f.name}` | `{f.type}` | {f.metadata[b"role"].decode()} |'
                  for f in schema]
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    print(f'{len(SCHEMA) + len(AWARD_SCHEMA)} fields -> {path}')


def _yyyymmdd(value, flag):
    if value is None:
        return None
    if not (value.isdigit() and len(value) == 8):
        raise SystemExit(f'{flag} must be YYYYMMDD, got {value!r}')
    return datetime.date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def scope_filter(rows, cpvs, nuts, date_from, date_to):
    """Narrow a parsed corpus to an analysis scope — a trade, a region, a period.

    Deliberately separate from what bulk.py/download.py filter on. Acquisition can only
    express what the source can: country, CPV and publication date. "Bridges in Hesse" is
    neither — TED's packages carry no region filter, and which CPV subdivision matters is
    usually decided after seeing the data. Refining here means changing your mind costs a
    re-run over local XML instead of a re-download.

    CPV matches the lot's MAIN classification only. The additional codes list secondary
    trades, so matching them would pull a school building into a bridge analysis because
    it includes a footbridge. Acquisition already did the loose match.

    NUTS matches the place of performance, falling back to the buyer's region for lots
    that state no place (~1%). A federal buyer in Berlin can procure a bridge in Hesse.

    Dates match publication_date — the same field acquisition selected on — falling back
    to issue_date, which can precede publication by days.
    """
    kept = []
    for row in rows:
        if cpvs and not (row.get('cpv_main') or '').startswith(tuple(cpvs)):
            continue
        if nuts:
            region = row.get('place_nuts3') or row.get('buyer_nuts') or ''
            if not region.startswith(tuple(nuts)):
                continue
        when = row.get('publication_date') or row.get('issue_date')
        if date_from and (when is None or when < date_from):
            continue
        if date_to and (when is None or when > date_to):
            continue
        kept.append(row)
    return kept


def scope_tag(cpvs, nuts, date_from, date_to, use_all):
    """A filename fragment describing the scope, so two analyses cannot overwrite
    each other's output."""
    if use_all:
        return 'all'
    parts = []
    if cpvs:
        parts.append('cpv' + '-'.join(cpvs))
    if nuts:
        parts.append('nuts' + '-'.join(nuts))
    if date_from or date_to:
        parts.append(f"{date_from.strftime('%Y%m%d') if date_from else 'start'}"
                     f"-{date_to.strftime('%Y%m%d') if date_to else 'end'}")
    return '_'.join(parts)


def _coverage(table, schema, title):
    print(f'\n  {title}')
    for field in schema.names:
        col = table.column(field)
        if pa.types.is_list(col.type):
            filled = sum(1 for v in col.to_pylist() if v)
        else:
            filled = len(col) - col.null_count
        print(f'    {field:30s} {100 * filled / max(len(table), 1):5.1f}%')


def log_dir(xml_dir):
    """Where this run's reports go: the state directory, never the process's
    working directory.

    `--xml-dir` is always `<state>/raw/xml`, so its grandparent is the state
    root. Deriving the path that way leaves the laptop untouched — run from
    the checkout, `data/raw/xml` still resolves to `data/logs` — while in the
    container it follows the mount to `/data/logs`.

    The relative `'data/logs'` this replaces worked only because the laptop
    happens to run the cycle from the checkout. In the container the working
    directory is `/app`, owned by root, and the cycle runs as `tm`: creating a
    relative `data/` there raises PermissionError, and since `loop.py` runs
    this module with `check=True`, that took the entire weekly cycle down with
    it — after the store had been rewritten, before anything was graded,
    trained or delivered.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(xml_dir)))
    return os.path.join(root, 'logs')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--xml-dir', default='data/raw/xml',
                    help='input folder of notice XML (default: data/raw/xml)')
    ap.add_argument('--cpv', default=None, metavar='CODES',
                    help='keep lots whose MAIN CPV starts with any of these, comma-'
                         'separated (e.g. 45221 for bridges)')
    ap.add_argument('--nuts', default=None, metavar='CODES',
                    help='keep lots whose place of performance starts with any of these '
                         "(e.g. DE7 for Hesse); falls back to the buyer's region")
    ap.add_argument('--from', dest='date_from', default=None, metavar='YYYYMMDD',
                    help='keep notices published on or after this date')
    ap.add_argument('--to', dest='date_to', default=None, metavar='YYYYMMDD',
                    help='keep notices published on or before this date')
    ap.add_argument('--all', action='store_true',
                    help='use every notice in the folder — say so deliberately')
    ap.add_argument('--tenders-out', default=None,
                    help='default: data/tenders_<scope>.parquet')
    ap.add_argument('--awards-out', default=None,
                    help='default: data/awards_<scope>.parquet')
    ap.add_argument('--limit', type=int, default=0, help='parse only the first N files')
    ap.add_argument('--coverage', action='store_true', help='print per-column fill rates')
    ap.add_argument('--strict', action='store_true',
                    help='re-raise on the first unparseable file instead of skipping it')
    ap.add_argument('--deduplicate', action='store_true',
                    help='collapse each lot to its latest revision (default: keep every '
                         'revision, so n_corrections_so_far varies within a lot)')
    ap.add_argument('--fields-doc', nargs='?', const='FIELDS.md', default=None,
                    metavar='PATH', help='write the field dictionary and exit')
    args = ap.parse_args()

    if args.fields_doc:
        write_fields_doc(args.fields_doc)
        return

    cpvs = [c.strip() for c in args.cpv.split(',')] if args.cpv else None
    nuts = [n.strip().upper() for n in args.nuts.split(',')] if args.nuts else None
    date_from = _yyyymmdd(args.date_from, '--from')
    date_to = _yyyymmdd(args.date_to, '--to')
    # The XML folder is deliberately broader than any one analysis, so forgetting a
    # filter is the likeliest mistake and the hardest to spot: nothing crashes, and a
    # parquet mixing IT projects into a construction study looks entirely normal.
    # Requiring the flag turns a silent wrong answer into a loud question.
    if not any((cpvs, nuts, date_from, date_to, args.all)):
        raise SystemExit(
            'error: no scope given. Say what you want to analyse:\n'
            '  --cpv 45221            bridges\n'
            '  --nuts DE7             Hesse\n'
            '  --from 20260401 --to 20260630   a period\n'
            '  --all                  everything in the folder, deliberately')
    if args.all and any((cpvs, nuts, date_from, date_to)):
        raise SystemExit('error: --all cannot be combined with a filter')

    tag = scope_tag(cpvs, nuts, date_from, date_to, args.all)
    tenders_out = args.tenders_out or f'data/tenders_{tag}.parquet'
    awards_out = args.awards_out or f'data/awards_{tag}.parquet'

    files = sorted(glob.glob(os.path.join(args.xml_dir, '*.xml')))
    if args.limit:
        files = files[:args.limit]
    if not files:
        raise SystemExit(f'no XML found in {args.xml_dir}')

    # Parsing is the slow phase and used to print nothing at all until it finished, so
    # a multi-minute build over tens of thousands of files looked like a hang.
    print(f'parsing {len(files)} files from {args.xml_dir} · scope [{tag}]')
    started = time.monotonic()

    # One pathological file must cost its own rows, never the run: catch everything,
    # log it, and keep going (--strict restores fail-fast for debugging).
    tender_rows, award_rows, failed = [], [], []
    for i, path in enumerate(files, 1):
        if i % 2000 == 0 or i == len(files):
            secs = time.monotonic() - started
            rate = i / max(secs, 0.001)
            eta = (len(files) - i) / max(rate, 0.001)
            print(f'  {i}/{len(files)} files · {len(tender_rows)} lots · '
                  f'{rate:.0f}/s · {secs:.0f}s elapsed'
                  + (f' · ~{eta:.0f}s left' if i < len(files) else ''))
        try:
            lots, awards = parse_notice(path)
        except Exception as exc:
            if args.strict:
                raise
            failed.append({'file': os.path.basename(path),
                           'error_type': type(exc).__name__,
                           'error': str(exc),
                           'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()})
            continue
        tender_rows.extend(lots)
        award_rows.extend(awards)

    if failed:
        logs = log_dir(args.xml_dir)
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, 'extract_failures.jsonl'), 'a', encoding='utf-8') as fh:
            for entry in failed:
                fh.write(json.dumps(entry) + '\n')

    # Must precede both filtering and deduplication: the count needs every revision of
    # the chain, including revisions the scope may later drop.
    add_correction_counts(tender_rows)

    if not args.all:
        before = len(tender_rows), len(award_rows)
        tender_rows = scope_filter(tender_rows, cpvs, nuts, date_from, date_to)
        # Awards follow their tender: an award for an out-of-scope lot is not part of
        # this analysis, and keeping it would leave rows that join to nothing.
        keys = {(r['procedure_id'], r['lot_id']) for r in tender_rows}
        award_rows = [r for r in award_rows
                      if (r['procedure_id'], r['lot_id']) in keys]
        print(f'scope [{tag}]: tenders {before[0]} -> {len(tender_rows)}, '
              f'awards {before[1]} -> {len(award_rows)}')
        if not tender_rows:
            print('  warning: the scope matched nothing — check --cpv/--nuts/--from/--to')

    if args.deduplicate:
        before = len(tender_rows), len(award_rows)
        tender_rows = deduplicate(tender_rows)
        award_rows = deduplicate(award_rows)
        print(f'corrigenda collapsed: tenders {before[0]} -> {len(tender_rows)}, '
              f'awards {before[1]} -> {len(award_rows)}')

    tenders = pa.Table.from_pylist(tender_rows, schema=SCHEMA)
    awards = pa.Table.from_pylist(award_rows, schema=AWARD_SCHEMA)
    for table, out in ((tenders, tenders_out), (awards, awards_out)):
        os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
        pq.write_table(table, out, compression='zstd')
        print(f'{len(table):5d} rows x {table.num_columns:3d} cols -> {out} '
              f'({os.path.getsize(out) / 1e6:.1f} MB)')

    print(f'\n{len(files)} files parsed, {len(failed)} unparseable')
    if failed:
        for entry in failed[:5]:
            print(f"  {entry['file']}: {entry['error_type']}: {entry['error']}")
        print('  full list appended to '
              f'{os.path.join(log_dir(args.xml_dir), "extract_failures.jsonl")}')

    flag_counts = collections.Counter(f for row in tender_rows for f in row['quality_flags'])
    flag_counts.update(f for row in award_rows for f in row['quality_flags'])
    if COERCIONS:
        print('\nnon-empty values coerced to null:')
        for (ctx, reason), n in sorted(COERCIONS.items()):
            print(f'  {ctx}: {reason} x{n}')
    if flag_counts:
        print('\nquality flags:')
        for flag, n in sorted(flag_counts.items()):
            print(f'  {flag} x{n}')

    report = {
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'xml_dir': args.xml_dir,
        'files': len(files),
        'failed': len(failed),
        'tender_rows': len(tenders),
        'award_rows': len(awards),
        'coercions': {f'{ctx}: {reason}': n for (ctx, reason), n in sorted(COERCIONS.items())},
        'quality_flags': dict(sorted(flag_counts.items())),
    }
    logs = log_dir(args.xml_dir)
    os.makedirs(logs, exist_ok=True)
    report_path = os.path.join(logs, 'extract_report.json')
    with open(report_path, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=2)
    print(f'\nrun report -> {report_path}')

    # The two tables only overlap where a call and its result were both downloaded —
    # that intersection, not either row count, is the trainable set.
    tender_keys = set(zip(tenders.column('procedure_id').to_pylist(),
                          tenders.column('lot_id').to_pylist()))
    award_keys = set(zip(awards.column('procedure_id').to_pylist(),
                         awards.column('lot_id').to_pylist()))
    # Count distinct keys, not rows: corrigenda republish the same lot, so row counts
    # overstate the sample. 3,415 tender rows are only ~2,300 distinct lots.
    joined = tender_keys & award_keys
    labelled = {k for k, n in zip(zip(awards.column('procedure_id').to_pylist(),
                                      awards.column('lot_id').to_pylist()),
                                  awards.column('n_tenders').to_pylist())
                if k in joined and n is not None}
    counts = sorted(n for k, n in zip(zip(awards.column('procedure_id').to_pylist(),
                                          awards.column('lot_id').to_pylist()),
                                      awards.column('n_tenders').to_pylist())
                    if k in labelled and n is not None)
    print(f'  distinct lots: {len(tender_keys)} tender, {len(award_keys)} award, '
          f'{len(joined)} joinable')
    print(f'  ... of which carry a bidder count: {len(labelled)}  <-- trainable set')
    if counts:
        counts.sort()
        print(f'  bidder count: min={counts[0]} median={counts[len(counts) // 2]} '
              f'max={counts[-1]}  single-bid={100 * sum(c == 1 for c in counts) / len(counts):.1f}%')

    if args.coverage:
        _coverage(tenders, SCHEMA, 'tenders.parquet')
        _coverage(awards, AWARD_SCHEMA, 'awards.parquet')


if __name__ == '__main__':
    main()
