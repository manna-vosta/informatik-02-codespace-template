# replace-openehr-template-in-form

Takes a Better EHR Studio form **package** zip (the nested format with `package-manifest.json` + inner form zip + OPT), swaps in a new OPT via `--new_opt`, updates `templateId` / OPT `<template_id>` and form display/file names, then writes a correctly re-packed package zip with refreshed SHA-256 hashes.

## Background
Changing the name of an openEHR template inside an already existing form (e.g. if you want two major versions in your system) may force you to rebuild all form details and automations even though the new template is almost the same content. This tool helps you swap template without wrecking the rest of the form. 

## Prerequisites

- **Python 3.9 or later**
- The following files have to be available locally in the same dir where you're running the script:
  - the form zip that you want to update
  - the OPT file for the new template

## Running the script

```sh
python3 ./replace_openehr_template_in_form.py \
  <form zip file> \
  --new_opt <new OPT file> \
  --new_template_id <new template_id> \
  --new_form_name <new form name> \
  --new_form_display_name <new form display name>
```

Example:

```sh
python3 ./replace_openehr_template_in_form.py \
  TEST_update_templateID_MDK_Lungcancer_Oscar_2_2_27_FORM.zip \
  --new_opt Diagnostic_MDT_Lung_cancer_TEMPLATE.v2.opt \
  --new_template_id Diagnostic_MDT_Lung_cancer.v2 \
  --new_form_name TEST_update_templateID_MDK_Lungcancer_Oscar_2_2_27_FORM_UPDATED \
  --new_form_display_name "TEST update templateID MDK Lungcancer Oscar UPDATED"
```

## Running the tests

```sh
./test_replace_openehr_template_in_form.py
```
