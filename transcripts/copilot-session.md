## Five changes implemented

### 1. ICP-only position estimation

The previous pipeline allowed neighbourhood-derived drift estimates to modify parcel position after image matching.

A parcel could therefore receive a final correction influenced partly by nearby parcels rather than solely by the imagery evidence associated with that parcel.

The neighbourhood correction path has been removed.

```python
final_shift = icp_shift
```

Parcel position is now determined exclusively by `_icp_shift()`.

Neighbourhood information is still retained, but only for confidence estimation.

This removes the failure mode where local consensus can override a valid imagery match and enforces a strict separation between:

```text
position estimation
```

and

```text
confidence estimation
```

Position is now derived only from observable boundary evidence.

---

### 2. Bootstrap-safe anchor promotion

The previous L2 promotion logic required neighbourhood agreement before a parcel could become an anchor.

With only six ground-truth parcels available, this created a bootstrap dependency:

```text
need anchors
    to create anchors
```

Large regions of the village therefore remained unable to contribute anchors until neighbouring anchors already existed.

Anchor promotion now depends entirely on parcel-local evidence:

```text
strong ICP alignment
high post-shift inlier fraction
usable imagery signal
plausible area consistency
```

Neighbourhood agreement no longer participates in anchor creation.

This allows image-supported corrections to seed the anchor network directly and prevents anchor growth from being gated by existing anchor density.

---

### 3. Distribution-based neighbourhood agreement

The previous neighbourhood term compared each parcel against a single interpolated drift estimate.

This reduced spatial validation to:

```text
Does this parcel match the prediction?
```

Neighbourhood validation has been replaced with `_zscore_agreement()`.

Rather than comparing against a single predicted shift, candidate corrections are evaluated against the local anchor distribution.

Conceptually:

```text
candidate shift
      vs
local anchor behaviour
```

Parcels whose shifts resemble nearby trusted anchors receive high agreement scores.

Parcels whose shifts are unusual relative to nearby anchors receive progressively lower scores.

This changes neighbourhood validation from a prediction problem into an anomaly-detection problem and provides substantially better calibration resolution.

Neighbourhood statistics now answer:

```text
How unusual is this correction?
```

rather than:

```text
What correction should be applied?
```

---

### 4. Post-ICP alignment validation

The previous workflow treated the ICP solution itself as the primary indicator of correction quality.

This was insufficient because finding a displacement and finding a useful displacement are not equivalent conditions.

The corrected parcel is now re-evaluated after shifting.

Boundary samples are tested against the edge map and a post-shift inlier fraction is computed:

```text
aligned boundary samples
------------------------
total boundary samples
```

Corrections are therefore evaluated on the quality of the final alignment rather than on the existence of a candidate shift.

This distinguishes between:

```text
ICP found a shift
```

and

```text
ICP found a shift that actually improves boundary alignment
```

which are materially different outcomes.

---

### 5. Boundary densification

The original ICP implementation relied primarily on parcel vertices.

For simple cadastral polygons this often produced only a small number of correspondence samples.

Boundary sampling is now densified prior to matching.

A parcel that previously contributed only corner vertices now contributes samples along the full boundary:

```text
4-corner parcel
    ↓
dozens of boundary samples
```

rather than:

```text
4-corner parcel
    ↓
4 matching samples
```

This allows long boundary segments to influence the displacement estimate proportionally instead of allowing corner placement to dominate the solution.

The resulting shift estimate is substantially more stable on large agricultural parcels.

---

### 6. Area consistency as evidence rather than a filter

The previous implementation treated area mismatch primarily as a filtering mechanism.

The challenge description explicitly states:

```text
drawn ÷ recorded ratio is a clue, not a verdict
```

Area consistency is therefore used as confidence evidence rather than as a hard decision boundary.

The recorded parcel extent is computed as:

```text
recorded area
+
pot-kharaba
```

and compared against the mapped parcel area.

Parcels with plausible ratios receive confidence support.

Parcels with increasingly implausible ratios receive progressively stronger penalties.

Area consistency can therefore influence confidence but cannot determine parcel position.

---

## Why these changes

The challenge distinguishes between two independent failure modes:

```text
H1 = placement error

H2 = geometry error
```

A placement error can be corrected because the correct boundary location remains visible in imagery.

A geometry error cannot be corrected through translation because the parcel shape itself disagrees with the underlying record.

The previous architecture allowed neighbourhood-derived information to influence both position and confidence.

The revised architecture assigns a single responsibility to each signal:

```text
imagery
    → position

post-shift alignment
    → confidence

neighbourhood agreement
    → confidence

area consistency
    → confidence

imagery quality
    → confidence
```

This ensures that positional corrections remain tied to observable field evidence while auxiliary signals are used only to assess trustworthiness.

The resulting confidence score measures:

```text
How likely is this parcel to represent a recoverable placement error?
```

rather than:

```text
How strongly does the neighbourhood support this correction?
```

which is more closely aligned with the challenge objective.

---

### Questions intentionally left out

**Why not use neighbour interpolation to improve parcel position?**

Neighbourhood behaviour can indicate whether a correction is plausible but does not provide direct evidence of boundary location.

**Why not require neighbour agreement for anchor promotion?**

Doing so creates a bootstrap dependency that prevents anchor growth in sparsely anchored regions.

**Why not use area ratio to estimate parcel movement?**

Area consistency contains no directional information and therefore cannot determine where a parcel should move.

**Why not classify parcels directly as correct or incorrect?**

The challenge evaluates confidence calibration in addition to geometric correction quality. Continuous confidence scores preserve substantially more ranking information than binary decisions.




https://claude.ai/share/9653aef8-1c3d-451c-b6be-ca84a198fd0b