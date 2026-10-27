# Real documents

Put real or phone-photographed documents here, one folder per case: `eval/real/<case>/*.pdf`.
Optionally add `expected.json` in the case folder: `{"land_deed.survey_no": "Verified", ...}`.
They are reported in a separate table by `python -m eval.run` (or `--real-only`), never mixed with the synthetic numbers.
