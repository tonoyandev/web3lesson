# Roadmap

Where anti-persona stands today, who could really use it, and what it takes to
turn it from a working prototype into a product people need.

## Where we are

anti-persona is a strong prototype. It reads your traces locally, builds a
persona, plans a gradual path with measurable checks, runs it in a browser, and
ships as an app for macOS and Windows.

It is not a product yet, for two reasons:

- **It does not measure the effect where it matters.** The metrics look at the
  app's own browser profile. They don't look at your real YouTube feed, your
  real search suggestions, or what ChatGPT, Claude and Gemini remember about you.
- **Its AI channels depend on automation the services forbid.** Driving the
  ChatGPT, Claude and Gemini websites with a bot is against their terms. It also
  breaks with every redesign. Nobody has shown that it changes an assistant's
  memory.

**The shift.** The promise changes from *"become your opposite"* to *"see what
algorithms know about you, then retrain your feeds and AIs toward the person you
want to be."*

## Who needs it

| Who | Their problem | What they need from us | Demand |
|---|---|---|---|
| People tired of their feeds | YouTube and search keep showing the same things; the algorithm is stuck | retrain the feed toward chosen interests, and see the change in the feed itself | high |
| Digital literacy: schools, universities, security awareness at companies | people don't see how much their traces reveal | the "who am I, according to my data" part, with explanations, without automation | high, sold to organisations |
| Privacy-minded people | they don't want a precise profile at ad networks and AIs | noise in the profile, AI memory cleanup, control over their data | medium, niche |
| People changing habits | they want a different life, but their surroundings pull them back | surroundings that follow their goal, plus real-life metrics | medium, strong competitors |
| Researchers auditing algorithms | they need repeatable recommendation audits | controlled profiles, feed snapshots, a full action log | small, but pays |

The first two fit best. Digital literacy is almost ready today. Feed retraining
needs the missing measurement.

## What to build

Priority: **P0** is needed for the first real release, **P1** for version 1,
and **P2** comes later. Effort is in weeks for one developer.

### Integrations

| What | Why | Priority | Effort |
|---|---|---|---|
| Browser extension for Chrome, Firefox and Safari | work inside the person's real browser: real history with permission, suggestions, feed snapshots. Replaces the Playwright profile | P0 | 3–4 |
| Google Takeout import: search, YouTube, Chrome on every device | research across a whole life, not one laptop | P0 | 1–2 |
| Official ChatGPT, Claude and Gemini data exports | the allowed way to read chats, instead of app folders that are often encrypted | P0 | 1 |
| Snapshots of the YouTube home feed and Google search suggestions | measure the change where it matters | P0 | 2–3 |
| YouTube Data API through OAuth | subscriptions, playlists and likes the official way. Google's app review takes 2–6 weeks | P1 | 2 |
| TikTok, Instagram and Reddit data exports | the mobile feeds where people spend most of their time | P1 | 2 |
| AI memory | once a week, ask each AI "what do you know about me?", then prepare memory and instruction edits that the person pastes in themselves | P1 | 1–2 |
| Optional cloud model, with consent | a plan in seconds instead of 20 minutes, and support for weak laptops | P1 | 1 |
| Spotify Web API | music shapes a profile strongly, and the API allows follows and playlists | P2 | 1–2 |
| Screen Time, Apple Health, Google Fit | real-life metrics: less screen time, more steps outside | P2 | 2 |
| Mobile companion for iOS and Android | tasks on the phone, where automation is not possible | P2 | 6–8 |
| Telegram or email notifications | a short plan for the day | P2 | 1 |

### Logic

| What | Why | Priority | Effort |
|---|---|---|---|
| Closed loop: plan, act, measure the real feed, adjust the pace | the pace is fixed today. A controller should speed up or slow down based on real results | P0 | 2–3 |
| The person checks their persona: "that's me" or "not me" for each trait | the model makes mistakes, and the plan is built on them | P0 | 1 |
| Goal templates instead of a mirror | "fewer anxious news", "from endless scrolling to learning", "more nature" | P0 | 1 |
| "You do it" mode by default | the app suggests actions and the person does them. Automation stays optional | P0 | 2 |
| Baseline and undo | a snapshot of the profile before the start, and a way back to it | P1 | 1 |
| Clean-up guide | step-by-step removal of the activity from Google's My Activity and from AI memory | P1 | 1 |
| Several people on one computer | separate profiles, and consent from each person | P2 | 1–2 |

### Safety and legal

| What | Why | Priority | Effort |
|---|---|---|---|
| Block harmful goals | a filter on goals and queries: extremism, self-harm, eating disorders, drugs | P0 | 2 |
| Careful mode for sensitive topics | health, religion, politics and sexuality need separate consent | P0 | 1 |
| Encrypt personas and plans | the persona is the most sensitive file. Keep the key in Keychain or Credential Manager, and offer export and delete in one click | P0 | 1 |
| Sign the apps | unsigned apps stop people at system warnings. Apple costs 99 USD a year; Windows needs its own certificate | P0 | 1 |
| Adults only | profiling and account automation | P0 | under 1 |
| Privacy policy, threat model, outside security audit | required before a public launch | P1 | 2 |

## What to drop or rework

- **Automating ChatGPT, Claude and Gemini through their websites.** Replace it
  with the allowed path: ask each AI what it remembers, and prepare edits the
  person makes themselves.
- **Flags that hide automation from websites.** They are fine for a class
  project, but a reputation risk for a product.
- **A separate Chrome profile as the main path.** Without the same account it
  barely touches the real feeds. With the same account it runs into Google's
  terms. A browser extension in the real browser solves both.
- **A 17 GB model as the default.** Offer a 5 GB option, and a cloud option with
  consent.

## Plan

1. **Test demand. 2 weeks.** Run 15–20 interviews in two groups: people who
   want to retrain their feeds, and teachers of digital literacy. Continue if
   at least 40% would use it every week.
2. **MVP. 6–8 weeks.** The browser extension, Takeout import, YouTube feed
   snapshots, the closed loop, goal templates, the persona check and the
   harmful-goal filter.
3. **Version 1. About 8 more weeks.** YouTube API, AI memory, social media
   exports, the optional cloud model, signed apps and encryption.
4. **Mobile companion. 6–8 weeks.** Only if the MVP keeps people coming back.

With two developers and a half-time designer, version 1 takes 4–6 months.

## How we know it works

| Metric | Target |
|---|---|
| Share of goal topics in the real YouTube feed after 4 weeks | a clear rise from the baseline |
| Persona traits the person confirms as true | at least 80% |
| People still active in week 4 | at least 40% |
| Accounts blocked by any service | 0 |

## Contributing to the roadmap

Pick an item, open an issue with its name, and say how you want to build it.
Items marked P0 come first. Ideas that make the tool safer or more honest are
always welcome.
