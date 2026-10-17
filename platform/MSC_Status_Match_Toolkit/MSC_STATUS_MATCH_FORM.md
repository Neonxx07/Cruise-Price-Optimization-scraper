# MSC Voyagers Club Status Match Form

Inspected 2026-10-08 at
<https://www.msccruisesusa.com/msc-voyagers-club/status-match>.
This is a consumer application to request a match from another cruise line,
hotel, or tour-operator loyalty program. MSC says it will review the request
and contact the applicant; approval provides a matched Voyagers Club tier and
benefits, including a 5% discount on future MSC sailings. It is not a tool for
applying a discount to a currently scanned MSC booking.

## Fields observed

| Section | Field | Required | MSC booking capture |
|---|---|---:|---|
| Personal information | First name | Yes | A passenger full name is captured; splitting it into first and last name must be verified. |
| Personal information | Last name | Yes | Same as above. |
| Personal information | Date of birth (`MM/DD/YYYY`) | Yes | Captured for each listed passenger. |
| Personal information | Mobile number | Yes | Not captured. Applicant must provide it. |
| Personal information | Email | Yes | Not captured. Applicant must provide it. |
| Address | Country (page showed United States and Puerto Rico) | Yes | Not captured. |
| Address | ZIP code | Yes | Not captured. |
| Address | City (enabled/populated after address lookup) | Yes | Not captured. |
| Current loyalty card | Company/program | Yes | Not captured. The passenger's MSC Voyagers details are not the external program being matched. |
| Current loyalty card | Membership status/tier | Yes | Not captured for another loyalty program. Options depend on the selected company. |
| Current loyalty card | Membership card number | Yes | Not captured for another loyalty program. |
| Current loyalty card | Card scan or screenshot upload | Yes | The unified workflow captures a genuine GoCCL guest-info view; MSC may still require a separate card image, so a human must check acceptability. The local template preview is never evidence. |
| Sign and confirm | Signature (name and surname) | Yes | Not captured; applicant must review and authorize the attestation. |
| Sign and confirm | Acceptance of terms and accuracy statement | Yes | Applicant must review and accept. Do not automate acceptance. |
| Optional preferences | Marketing communications | No | Not captured; leave to applicant. |
| Optional preferences | Personalized communications | No | Not captured; leave to applicant. |

The page accepted a browser-only field-entry test with synthetic values
(`TEST EXAMPLE`, `01/01/1990`, and `202-555-0100`). The form formatted the
phone number. No other form sections were submitted, and the Confirm button
was not clicked. Browser interaction with a public form may still be observed
by the website before submission.

## Headless field-entry check (2026-10-08)

The live page returned HTTP 200 in standard headless Chromium. A non-submitting
Playwright test filled test-only name, DOB, phone, email, ZIP, and city fields.
After the ZIP lookup, the visible DOB remained `01/01/1990`, the form's hidden
date value was `1990-01-01`, and ZIP `74875` resolved to `Tryon`. The page URL
did not change and no form submission was made.

This verifies that headless entry is technically possible; it does not make
fictional contact details or a generated address suitable for an actual
application. Do not use disposable email or generated fake-address services
for real status-match applications.

## Local headless prefill helper

`msc_status_match_form.py` can prefill the personal and address fields
from a local JSON file. Create that file locally and supply accurate,
applicant-authorized values; do not put it in source control or share it.
The helper:

- validates the required values, `MM/DD/YYYY` DOB format, and ZIP format;
- fills DOB before the ZIP lookup (which synchronizes MSC's hidden date field),
  then verifies both the visible DOB and normalized hidden value;
- only fills loyalty company, status, number, and card proof when all four are
  explicitly supplied as verified applicant data;
- leaves signature and optional consent fields untouched;
- blocks the status-match form's submit event and non-read-only requests to the
  application endpoint, and never clicks Confirm;
- prints field names rather than personal values. An optional screenshot
  contains personal information and should be stored securely.
- optionally saves both a blank before-fill screenshot and a verified
  after-fill screenshot.

Required JSON keys: `first_name`, `last_name`, `date_of_birth`,
`mobile_number`, `email`, `country_code` (`US` or `PR`), and `zip_code`.
The mobile number must be a US/Puerto Rico number; the script verifies the
form's phone selector is set to `+1`.
`city` may be supplied when the ZIP resolves to multiple cities. Optional
loyalty keys, which must be supplied together, are `loyalty_company`,
`loyalty_status`, `membership_number`, and `card_proof_path`.

Run from the `platform` directory:

```powershell
& 'C:\cruisevenv\venv\Scripts\python.exe' MSC_Status_Match_Toolkit\msc_status_match_form.py `
  --data 'C:\private\msc-status-match-applicant.json' `
  --before-screenshot 'C:\private\msc-status-match-before.png' `
  --screenshot 'C:\private\msc-status-match-after.png'
```

The helper only prefills and verifies; it closes the headless browser after
verification. Review the optional screenshot and complete signature/consent
steps manually. Never treat this utility as proof of status-match eligibility
or submit unverified data.

## Unified live preparation workflow

`msc_status_match_workflow.py prepare` connects the existing guarded
`MscLiveService` passenger capture, `GoCCLScraper` guest-number lookup,
`core.vifp_guest` identity matching, and this form helper. It reads the
customer's actual Carnival Guest Number from explicit CLI input, captures the
unedited GoCCL account view, and requires an exact name/DOB match to one adult
on the freshly captured MSC booking. It does not click GoCCL's Add Number
button. Name, DOB, Carnival tier, membership number, and screenshot path are
derived from the portal captures; the caller's private contact JSON can
contain only mobile, email, country, ZIP, and city.

The integrated tool writes the genuine account screenshot, before/after form
screenshots, and a per-run JSON report only under `--output-dir`, which must
be outside the project. It stops for human review and does not sign, accept
terms, or submit. The GoCCL screenshot shows the account's guest information;
this does not prove MSC accepts that view instead of a separate card image.
The human reviewer must confirm the proof requirement and applicant-provided
contact details before any manual signature or submission.

For identity-only verification when contact or loyalty details are not
available, `msc_status_match_workflow.py identity-preview` opens the actual
MSC form and fills only booking-captured first name, last name, and DOB. All
contact, loyalty, proof, signature, and consent fields stay blank/untouched;
the script records that submission was blocked. It reports
`identity_preview_complete_not_application_ready`, not an application success.
The identity-only path refuses to use Guest Numbers in the reserved synthetic
`90xxxxxxxx` range in the full live-preparation workflow.

The GoCCL template filler is kept on the QA path only. It generates a local
synthetic HTML preview and must never be passed off as a genuine Carnival
account screenshot or attached to the live form.

## Full synthetic QA workflow

`msc_status_match_qa.py` tests the complete data path without opening the
live MSC application:

1. Read the latest existing passenger capture for the requested booking from
   `data/msc_control/booking_data.jsonl` (read-only; never deletes or rewrites
   captured data).
2. Require a chosen passenger index and verified adult age; use the captured
   first name, last name, and DOB.
3. Use headless Playwright to generate a fictional US address for the
   requested state.
4. Create a temporary Mail.tm test inbox via its REST API and poll only inbox
   metadata. No mail is sent, and message bodies are not retrieved.
5. Fill a local in-memory HTML mock form with the captured identity and
   synthetic test contact/address values; no request is made to MSC's form.
6. Verify the values, keep loyalty proof/signature/consent blank, ensure the
   mock Confirm button is disabled, and write the requested screenshot.

Example, from the `platform` directory:

```powershell
& 'C:\cruisevenv\venv\Scripts\python.exe' MSC_Status_Match_Toolkit\msc_status_match_qa.py `
  --booking 3001060 `
  --passenger-index 2 `
  --state Oklahoma `
  --screenshot 'C:\private\msc-status-match-local-qa.png'
```

The JSON report includes the temporary email address and generated address.
Treat the report and screenshot as sensitive because they also contain
captured passenger identity and DOB. Mail.tm inbox credentials remain only in
process memory; the account password is never printed or stored. The workflow
does not send email, retrieve message bodies, open the live MSC Status Match
site, or submit any form. A fresh passenger capture, if needed, uses the
project's `scan_passengers:<booking-id>` command and appends a new JSONL row.

This local QA flow is not proof of an external loyalty tier and is not an
application. Never reuse its fake contact/address data or temporary mailbox in
the actual MSC application. A private/incognito browser would still send
interactions to MSC and does not make fake applicant data private from MSC.

The end-to-end run completed on 2026-10-09: the address service produced an
Oklahoma test address, Mail.tm provisioned an inbox and reported zero messages
during the 10-second poll, and the populated identity/contact/address fields
verified in the local mock. Loyalty fields, card proof, signature, and optional
preferences remained blank or unchecked; the mock Confirm control was
disabled. No email was sent, no live MSC form was opened, no form was
submitted, and the capture corpus was not modified.

## What CruiseIntel has captured

The latest local `data/msc_control/booking_data.jsonl` records were measured
on 2026-10-08: 136 latest booking records containing 228 passenger records.
All 228 passenger records contained a name, date of birth, and age. Only 2
contained the additional title, gender, and state/province fields read from a
passenger form. Those are MSC booking/passenger details, not proof of status
in a different loyalty program.

Therefore the booking corpus can help prefill a verified applicant name and
DOB after the passenger is identified, but it cannot complete this
application. Mobile, email, country, ZIP, city, external loyalty provider,
external tier, external card number, and the actual card image are missing.
Signature and consent must remain with the applicant.

## Safe automation boundary

The form is technically browser-fillable with Playwright; use the local QA
mock for synthetic data. The live-form helper is only for accurate,
applicant-authorized data and verified loyalty proof. Never substitute
synthetic values or generated fake addresses for real contact/address data or
loyalty proof. Do not preselect optional marketing checkboxes.

MSC booking eligibility is a separate gate: CruiseIntel marks a booking
eligible for consideration only when it has verified no applied discount and
no existing MSC Voyagers membership. That verdict does not establish that an
applicant qualifies for MSC's external-loyalty status match; MSC makes that
decision after its own application review.
