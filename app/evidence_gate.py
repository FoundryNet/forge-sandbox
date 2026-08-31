"""Evidence scoring gate.

A resolution is a CLAIM: "this tag means this field." Every claim has to pay
for its confidence with measurable evidence. The score decides the confidence
-- not the match type, and not whichever layer happened to answer first.

The target is one specific failure mode: WRONG WITH CONFIDENCE. A tag that
folds onto a plausible-looking field and ships at 0.95 is worse than the same
tag coming back UNRESOLVED, because the caller cannot tell it from a real
answer. Below the threshold this gate refuses and says exactly what was
missing, which is a better answer than a confident guess.

Three deliberate departures from the scoring sketch, each load-bearing:

1. An EXACT corpus row bypasses the gate at 1.0. Scored literally it earns +40
   and lands in the "moderate" band, so the vendor's own documented mapping
   would ship less confidently than a keyword guess with a unit on it. An exact
   row is a known fact, not evidence to be weighed.

2. `generic_keyword` and `no_unit_evidence` penalise weak INFERENCE, so they do
   not apply to a row out of the caller's own pack. `ALARM -> alarm_code` is
   documented by the vendor; docking it for the word "alarm" being generic and
   for carrying no unit refuses a mapping that is certainly right. Inference
   still pays both penalties -- which is what keeps `DPF_Status ->
   execution_state` refused, since that one is a guess and not a row.

3. `quantity_agrees` can be earned without a unit token. The signal classifier
   matches quantity words ("speed", "temperature") from a curated vocabulary,
   and that a field's declared physical quantity belongs to the same family is
   real corroboration. Requiring a unit for it would refuse `SpindleSpeed`.
"""

EVIDENCE_WEIGHTS = {
    # Positive signals
    "exact_corpus":         40,   # known OEM+tag -> field mapping
    "pack_match":           30,   # OEM family pack match
    "keyword_alignment":    20,   # tag keywords match field name
    "unit_token_match":     15,   # stripped unit matches field unit
    "quantity_agrees":      10,   # dimensional / quantity agreement
    "domain_agrees":        10,   # OEM vertical matches field vertical
    "multi_keyword":        10,   # 2+ keywords align (SpindleSpeed = 2)
    "path_parent_context":   5,   # topic parent reinforces leaf meaning

    # Negative signals
    "domain_disagrees":    -20,   # OEM vertical != field vertical
    "single_letter_match": -30,   # one character of evidence
    "fold_ambiguous":      -20,   # multiple fold candidates
    "pint_mismatch":       -40,   # dimensional incompatibility
    "no_unit_evidence":     -5,   # tag carries no unit token at all
    "generic_keyword":     -10,   # "Total", "Status", "Value" -- too vague
    "cross_oem_borrow":    -15,   # matched via a different OEM's pack
    "cross_pack":           20,   # another vendor's pack documents this tag
}

CONFIDENCE_THRESHOLDS = {
    "resolve_full":   70,   # -> confidence 1.0
    "resolve_high":   50,   # -> confidence 0.85
    # 30, not 35. With these weights a tag whose words NAME the field in two
    # parts and carries no unit suffix tops out at exactly 30 -- `Vibration_RMS`
    # -> `vibration_rms`, `TotalHours` -> `operating_hours`. At 35 those are
    # unreachable, and so is every canonical field the schema left without
    # `physical_quantity` metadata. 30 admits them at 0.65 while still refusing
    # DPF_Status (-15) and every domain/physics conflict.
    "resolve_medium": 30,   # -> confidence 0.65, flagged as uncertain
    "refuse":          0,   # -> UNRESOLVED, reason: insufficient evidence
}

# Words that name a slot rather than a measurement. On their own they are not
# evidence -- half the tags in any plant contain one.
GENERIC_KEYWORDS = {
    "total", "status", "value", "reading", "sensor",
    "input", "output", "data", "signal", "level",
    "rate", "count", "mode", "state", "fault",
    "alarm", "error", "flag", "command", "setpoint",
    "pv", "sp", "cv", "mv", "sv",
}

# Match types that come from a written-down mapping rather than inference.
# These are exempt from the two penalties aimed at weak inference.
_DOCUMENTED = {"pack_match", "corpus_folded"}

# A cross-VENDOR row is still a curated mapping somebody wrote down: weaker than
# the caller's own family, stronger than pure inference. Scoring it at zero left
# correct mappings stranded -- `M_AC_PF` -> `power_factor` had keyword and domain
# evidence and still could not clear the bar, because the row itself counted for
# nothing. The signal classifier stays at zero base by design: it infers, it does
# not document.
_BORROWED = {"cross_oem", "cross_oem_unit_evidence"}


def apply_threshold(score):
    """(resolve, confidence, evidence_class) for a score."""
    if score >= CONFIDENCE_THRESHOLDS["resolve_full"]:
        return True, 1.0, "strong"
    if score >= CONFIDENCE_THRESHOLDS["resolve_high"]:
        return True, 0.85, "high"
    if score >= CONFIDENCE_THRESHOLDS["resolve_medium"]:
        return True, 0.65, "moderate"
    return False, 0.0, "refused"


def score_resolution(raw_tag, candidate_field, match_type, match_details,
                     oem_domain, field_spec, field_domain=None):
    """Score the evidence for a proposed tag -> field resolution.

    `match_details` carries what the pipeline already established:
      keyword_overlap    set of words shared by tag and field
      tag_unit           unit token found on the tag, if any
      pint_compatible    True / False / None (None = could not be judged)
      quantity_agrees    quantity channels agree
      matched_token      the token the subject match rested on
      fold_candidates    how many candidates the fold produced
      matched_oem        pack the row came from, when not the caller's
      path_parent        a parent path segment corroborates the leaf
    """
    score = 0
    reasons = []
    md = match_details or {}

    # ── Exact corpus is a known fact, not evidence to weigh ──────────────────
    if match_type in ("exact_corpus", "deterministic", "identity"):
        return {"score": EVIDENCE_WEIGHTS["exact_corpus"], "confidence": 1.0,
                "resolve": True, "reasons": ["+40 exact corpus match"],
                "evidence_class": "exact"}

    documented = match_type in _DOCUMENTED
    if documented:
        score += EVIDENCE_WEIGHTS["pack_match"]
        reasons.append("+30 pack match")
    elif match_type in _BORROWED:
        score += EVIDENCE_WEIGHTS["cross_pack"]
        reasons.append("+20 cross-vendor pack row")

    # ── Keyword evidence ─────────────────────────────────────────────────────
    overlap = set(md.get("keyword_overlap") or ())
    if len(overlap) >= 2:
        score += EVIDENCE_WEIGHTS["multi_keyword"]
        score += EVIDENCE_WEIGHTS["keyword_alignment"]
        reasons.append(f"+30 multi-keyword alignment: {sorted(overlap)}")
    elif len(overlap) == 1:
        kw = next(iter(overlap))
        if kw.lower() in GENERIC_KEYWORDS and not documented:
            score += EVIDENCE_WEIGHTS["generic_keyword"]
            reasons.append(f"-10 generic keyword only: {kw}")
        else:
            score += EVIDENCE_WEIGHTS["keyword_alignment"]
            reasons.append(f"+20 keyword alignment: {kw}")

    # ── Unit evidence ────────────────────────────────────────────────────────
    tag_unit = md.get("tag_unit")
    field_unit = (field_spec or {}).get("unit")
    pint_ok = md.get("pint_compatible")
    if tag_unit and field_unit:
        if pint_ok is False:
            score += EVIDENCE_WEIGHTS["pint_mismatch"]
            reasons.append(f"-40 dimensional mismatch: {tag_unit} != {field_unit}")
        elif pint_ok:
            score += EVIDENCE_WEIGHTS["unit_token_match"]
            score += EVIDENCE_WEIGHTS["quantity_agrees"]
            reasons.append(f"+25 unit and quantity agree: {tag_unit} -> {field_unit}")
    elif not tag_unit:
        corroborated = False
        if md.get("quantity_agrees"):
            # No unit, but the quantity channel still settled what is being
            # measured. That IS evidence about the quantity.
            score += EVIDENCE_WEIGHTS["quantity_agrees"]
            reasons.append("+10 quantity channel agrees")
            corroborated = True
        if len(overlap) >= 2:
            corroborated = True
        # The no-unit penalty exists to dock a tag that offers nothing but one
        # vague word. Applying it on top of multi-keyword or quantity evidence
        # docks the tag twice for a single missing signal.
        if not documented and not corroborated:
            score += EVIDENCE_WEIGHTS["no_unit_evidence"]
            reasons.append("-5 no unit token in tag")

    # ── Domain evidence ──────────────────────────────────────────────────────
    # Only evidence when BOTH sides name a domain. A universal field belongs to
    # everyone, and an unplaceable OEM is missing evidence rather than contrary
    # evidence -- which is what lets an unknown vendor degrade instead of die.
    if oem_domain and field_domain:
        if oem_domain == field_domain:
            score += EVIDENCE_WEIGHTS["domain_agrees"]
            reasons.append(f"+10 domain agrees: {oem_domain}")
        else:
            score += EVIDENCE_WEIGHTS["domain_disagrees"]
            reasons.append(f"-20 domain disagrees: {oem_domain} != {field_domain}")

    if md.get("path_parent"):
        score += EVIDENCE_WEIGHTS["path_parent_context"]
        reasons.append(f"+5 path parent corroborates: {md['path_parent']}")

    # ── Negative structural signals ──────────────────────────────────────────
    mt = md.get("matched_token")
    if mt and len(str(mt)) <= 1:
        score += EVIDENCE_WEIGHTS["single_letter_match"]
        reasons.append(f"-30 single-letter match: '{mt}'")

    fold_count = md.get("fold_candidates") or 1
    if fold_count > 1:
        score += EVIDENCE_WEIGHTS["fold_ambiguous"]
        reasons.append(f"-20 ambiguous fold: {fold_count} candidates")

    borrowed = md.get("matched_oem")
    if borrowed:
        score += EVIDENCE_WEIGHTS["cross_oem_borrow"]
        reasons.append(f"-15 cross-OEM borrow from {borrowed}")

    resolve, confidence, evidence_class = apply_threshold(score)
    return {"score": score, "confidence": confidence, "resolve": resolve,
            "reasons": reasons, "evidence_class": evidence_class}


def refusal_note(raw_tag, candidate_field, score, reasons):
    """Why a match that WAS found did not ship."""
    detail = "; ".join(reasons) if reasons else "no corroborating evidence"
    return (f"'{raw_tag}' matched '{candidate_field}', but the evidence scored "
            f"{score} (below {CONFIDENCE_THRESHOLDS['resolve_medium']}): "
            f"{detail}. Refused -- unresolved is safer than a wrong answer at a "
            f"confidence you would trust.")
