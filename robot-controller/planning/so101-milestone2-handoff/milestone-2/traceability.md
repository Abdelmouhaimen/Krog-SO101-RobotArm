# Requirements-to-tests traceability

| Requirements | Gate A tests | Gate B tests |
|---|---|---|
| REQ-01,17 | A22,A24 | B01,B04 |
| REQ-02,03 | A01,A02,A04 | B01 |
| REQ-04 | A05,A23 | B03 |
| REQ-05 | A03 | B02 |
| REQ-06 | A06,A07,A08 | B03 |
| REQ-07 | A08,A10,A11 | B07 |
| REQ-08 | A09,A17 | B05 |
| REQ-09 | A12,A21 | B04,B06,B07 |
| REQ-10,11 | A18,A19 | B08,B09 |
| REQ-12,13 | A13,A14,A15,A16 | B03,B04 |
| REQ-14,15 | A04,A17,A20 | B05,B10 |
| REQ-16 | A19,A20,A22 | B08,B09 |
| REQ-18 | A06,A23 | None (M3 future) |

`REQ-N` abbreviates `M2-REQ-N`; `A##` and `B##` abbreviate `M2-A##`/`M2-B##`. Treat every row as a coverage obligation, not evidence of test success.
