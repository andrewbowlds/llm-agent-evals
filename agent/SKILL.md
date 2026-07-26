---
name: rental-lead-autoresponder
description: >
  Automatically respond to new rental property lead emails from RentSpree, Zillow, and similar
  platforms. Use this skill whenever the user says "run the autoresponder", "respond to rental leads",
  "reply to new rental leads", "check for unread rental lead emails", "process rental lead
  notifications", or any time they want to handle incoming rental prospect inquiries. The skill finds
  unread rental lead notification emails, extracts prospect contact info and their questions, looks up
  full unit details from Firestore to answer those questions, and sends a personalized response
  directing the prospect to the prescreen. Always trigger this skill when the user mentions rental
  leads + any property address, even if they don't say "autoresponder" explicitly.
---

# Rental Lead Autoresponder

Processes unread rental lead emails, fetches full unit details from Firestore, answers the
prospect's specific questions inline, and directs them to the prescreen. Handles RentSpree and
Zillow rental notifications.

## Overview

EDP Realty rental funnel:
1. Prospect completes a **prescreen** on the property listing page (edprealty.com/rentals/{id})
2. If they pass -> they can immediately **self-schedule a self-guided tour** (combo provided for their time slot)
3. After touring, if they want to proceed -> **rental application** at $40 per adult applicant, then review

This skill handles the first touchpoint: reading what the prospect is actually asking, pulling
the relevant unit details from Firestore, and answering those questions in the response before
pointing them to the prescreen. A personalized response that answers real questions converts
better than a generic template.

Steps:
1. Find all unread rental lead notification emails in Gmail
2. Extract prospect email, address, name, and their message/questions
3. Check Firestore for full unit details
4. Skip prospects already replied to
5. Build a personalized response that answers their questions + directs to prescreen
6. Create the draft, archive the notification
7. Report results

---

## Step 1: Find Unread Rental Lead Emails

Run these Gmail searches:

  subject:"NEW LEAD" is:unread from:noreply-rentspree@rentspree.com
  from:no-reply@comet.zillow.com is:unread subject:"applied to your rental"

For each thread, fetch full content (messageFormat: FULL_CONTENT) and extract:

- **Prospect email**: RentSpree shows it twice -- use the version WITHOUT the "n" prefix
  (e.g. use john@gmail.com not njohn@gmail.com)
- **Property address**: in subject line or body
- **Prospect first name**: use if visible
- **Prospect's message**: the text the prospect actually wrote -- questions, concerns, anything
  they said beyond just expressing interest. This is the key input for personalizing the reply.

---

## Step 2: Skip if Already Replied

  from:andrew@edprealty.com to:{prospect_email} in:sent

If results exist, skip this prospect.

---

## Step 3: Fetch Full Unit Details from Firestore

Use get_rental_by_address with the property address. The result now includes:

  rentAmount, securityDepositAmount, beds, baths, squareFootage, yearBuilt,
  waterResponsibility, gasResponsibility, electricResponsibility, lawnResponsibility,
  trashResponsibility, internetResponsibility, parkingDetails, laundryType,
  appliancesIncluded, flooringType, heatingCoolingType, fencedYard, storageType,
  basementType, adaAccessible, furnished, smokingPolicy, leaseTermMonths,
  petsAllowed, catsAllowed, dogsAllowed, petPolicySummary, sec8Allowed, description

If available: true -> build a personalized response (Step 4) using listingUrl and these details.
If available: false -> send the unavailable response (see Step 4).
If found: false -> send the standard response linking to https://edprealty.com/rentals.

---

## Step 4: Build the Response

Use create_draft with replyToMessageId set to the notification message ID.
Set to: to the prospect's real email.

### When available: true

Read the prospect's message and identify what they're asking. Then check the Firestore fields
to see if you have the answer. Answer what you can confidently from the data; skip fields that
are null or "Not specified". Keep answers brief and factual -- one line each.

**Common questions and which Firestore fields answer them:**

| Prospect asks about... | Field(s) to use |
|------------------------|-----------------|
| Pets / dogs / cats     | catsAllowed, dogsAllowed, petPolicySummary |
| Section 8 / voucher    | sec8Allowed |
| Utilities / who pays   | waterResponsibility, gasResponsibility, electricResponsibility, lawnResponsibility, trashResponsibility, internetResponsibility |
| Parking                | parkingDetails |
| Laundry / washer dryer | laundryType |
| Appliances             | appliancesIncluded |
| Yard / fenced          | fencedYard |
| Square footage / size  | squareFootage |
| Rent / deposit         | rentAmount, securityDepositAmount |
| Lease length / term    | leaseTermMonths (0 = month-to-month) |
| Smoking                | smokingPolicy |
| Furnished              | furnished |
| Basement / storage     | basementType, storageType |
| Heating / AC           | heatingCoolingType |
| When available         | availableDate |

**Response format:**

  Subject: Re: [NEW LEAD] I'm interested in {address}.

  Hello{, FirstName},

  Thank you for your interest in {address}!

  {Only include this section if they asked questions AND you have answers from Firestore:}
  To answer your questions:
  - {Question topic}: {Answer from Firestore data}
  - {Question topic}: {Answer from Firestore data}
  {Omit any question you can't answer from available data -- don't guess}

  To get started, complete our quick pre-screen here: {listingUrl}

  If you qualify, you'll be able to self-schedule a tour right away. After your tour, if you'd
  like to move forward, the next step is a rental application -- $40 per adult applicant.

  Andrew

**If the prospect asked nothing specific** (just general interest, no questions), omit the
Q&A section entirely and go straight to the prescreen link.

### When available: false

  Subject: Re: [NEW LEAD] I'm interested in {address}.

  Hello{, FirstName},

  Thank you for your interest in {address}. Unfortunately this unit is no longer available.
  You can view our other available rentals at: https://edprealty.com/rentals

  Andrew

### When found: false (address not in Firestore)

  Subject: Re: [NEW LEAD] I'm interested in {address}.

  Hello{, FirstName},

  Thank you for your interest in {address}! You can view our available rentals and start a
  pre-screen here: https://edprealty.com/rentals

  Andrew

---

## Step 5: Archive the Notification

  tool: unlabel_thread
  threadId: {notification_thread_id}
  labelIds: ["UNREAD", "INBOX"]

---

## Step 6: Report Results

  Rental leads processed:
  - Responded to: [prospect email -- property -- questions answered]
  - Skipped (already replied): [list]
  - Skipped (unavailable): [list]
  - Skipped (not found in Firestore): [list]

---

## Edge Cases

- **Duplicate notifications**: RentSpree sends hourly reminders. The sent-mail check in Step 2
  handles this automatically after the first reply.
- **Applicants who already submitted**: If the notification is for someone already in a full
  application thread (e.g., "submitted documents" email), skip -- they're further in the funnel.
- **High volume**: Process all leads but note the count to the user.
- **Can't parse email**: Log as "could not parse" and move on.
- **Null fields**: Never mention a feature in the response if the Firestore value is null,
  empty, or "Not specified" -- only answer questions you actually have data for.
