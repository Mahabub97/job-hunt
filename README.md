# Job hunt desk

Every morning at 7:00 Dhaka time this finds new jobs for you: Product Manager and Senior Product
Manager roles, bank jobs (private, state-owned and Bangladesh Bank) and government jobs. It saves
the details to your own web page, where one click builds a resume and cover letter tailored to that
job, and a checklist tracks every application and deadline.

## What it costs: nothing

| Part | Service | Why it's free |
|---|---|---|
| Daily search robot | GitHub Actions | Free for public repositories |
| Your web page | GitHub Pages | Free for public repositories |
| Web search | Tavily | Free plan: 1,000 searches a month, no card. This uses about 300. |
| Reading, checking and scoring jobs, tailoring resumes | Google Gemini API | Free tier. Don't add billing to that Google account and it can never charge you. |
| Remote jobs | Remotive and Jobicy | Free public job feeds |

Free limits can change. If one is reached, the app keeps working with less: unchecked results are
saved and checked the next day, and tailoring asks you to try again later.

## How it finds jobs

1. **Official pages:** Bangladesh Bank's e-recruitment list (Bangladesh Bank and Bankers' Selection
   Committee circulars) and Teletalk's government job list.
2. **Web search:** ten searches a day covering private, bank and government circulars, bdjobs, and roles abroad.
3. **Remote job feeds:** Remotive and Jobicy, keeping only roles open to people in Bangladesh.

Gemini then reads every result, keeps only real open postings that match, and pulls out the deadline,
age limit, fee, vacancies, requirements and how to apply. Links always come from the source pages,
never from the AI.

## Setup (about 30 minutes, once)

### 1. Get a free Gemini key
1. Go to https://aistudio.google.com/apikey and sign in with a Google account.
2. Click **Create API key** and copy it.
3. Don't set up billing on this account.

### 2. Get a free Tavily key
1. Go to https://tavily.com and sign up. No card is needed.
2. Copy your API key from the dashboard.

### 3. Create the GitHub repository
1. Make a free account at https://github.com.
2. Click **New repository**. Name it `job-hunt`, choose **Public**, tick **Add a README file**, and click **Create repository**.

Public means the job list can be seen by anyone who has the address. Your resume, applications and
keys are never in the repository: the keys are stored as secrets, and your resume and checklist stay in your browser.

### 4. Upload the files
1. In the repository, click **Add file > Upload files**.
2. Drag in `config.json`, `README.md`, and the `scripts`, `docs` and `data` folders. Click **Commit changes**.
3. The workflow file is in a hidden folder, so add it by hand: click **Add file > Create new file**,
   type `.github/workflows/daily-jobs.yml` as the name (the slashes create the folders), paste in the
   contents of `daily-jobs.yml` (also provided as a separate file), and click **Commit changes**.

### 5. Add your secrets
Go to **Settings > Secrets and variables > Actions > New repository secret** and add each of these:

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | your Gemini key |
| `TAVILY_API_KEY` | your Tavily key |
| `CANDIDATE_PROFILE` | a few lines about you, used for fit scores (example below) |

Example profile (keep out your name, phone and email):
> Senior Product Manager with about 6 years at a travel-tech company in Dhaka, owning the flight
> product vertical. Strong in search and booking funnels, growth and working with engineering.
> BBA and MBA in Finance & Banking. Open to Product Manager and Senior Product Manager roles, bank
> roles (digital banking, Assistant Director, Senior Officer) and government roles for MBA graduates.

### 6. Turn on the web page
**Settings > Pages**. Under "Build and deployment", set Source to **Deploy from a branch**, Branch to
**main**, folder **/docs**, and click **Save**. After a minute your page is at
`https://YOUR-USERNAME.github.io/job-hunt/`. Bookmark it, and on your phone use "Add to Home Screen".

### 7. Run the first search
**Actions** tab > **Daily job search** > **Run workflow**. It takes 5 to 10 minutes. When it turns
green, open your page. After that it runs by itself every morning.

### 8. Set up the page (on each device you use)
1. **Settings:** paste your Gemini key and press **Test**. Add your date of birth for the age check.
2. **Resume:** add your contact details, then paste your full resume (or import it from a PDF).
3. **Settings > Load backup:** choose `my-applications.json` to bring in the applications you've already made.

To move everything to another device later, use **Download backup** and **Load backup**.

## Daily alerts (optional)

**Telegram:**
1. In Telegram, message **@BotFather**, send `/newbot`, follow the steps, and copy the token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/botYOUR-TOKEN/getUpdates` in a browser and find `"chat":{"id":` followed by a number.
4. Add secrets `TELEGRAM_BOT_TOKEN` (the token) and `TELEGRAM_CHAT_ID` (the number).

**Email (Gmail):**
1. Turn on 2-Step Verification for your Google account, then create an app password at https://myaccount.google.com/apppasswords.
2. Add secrets `EMAIL_USER` (your Gmail address) and `EMAIL_APP_PASSWORD` (the app password).
   Optionally add `EMAIL_TO` to send to a different address.

## Using it day to day

- **Jobs tab:** today's finds, with a fit score, deadline, age check, fee and requirements. Filter by
  Private, Bank, Government, Remote or Abroad.
- **Prepare application:** builds a tailored resume and cover letter for that job from your master
  resume, using only facts from your resume, and lists what the job asks for that your resume doesn't
  show. Edit it on the page, then **Save as PDF** or **Download Word**, open the application page, and press **I've applied**.
- **Applications tab:** your checklist and deadline countdown.

The final submit is always yours. Job sites and government portals need your login, fee payment,
photo and signature, and they don't allow automated applications, so the app takes you to the last
step instead.

## Changing what it looks for

Edit `config.json` on GitHub (open the file, then the pencil icon):

- `targets`: plain-English description of each kind of job you want. The AI reads this.
- `web_search.queries`: the daily searches. Keep 10 or fewer to stay inside the free plan.
- `official_pages`: official recruitment pages to read every day.
- `target_titles` and `exclude_title_words`: used to filter the remote job feeds.

## Privacy

- Your contact details are never sent anywhere. They're added to tailored resumes in your browser.
- The resume text and job details are sent to Google's Gemini free tier when you tailor. Google
  says free-tier content may be used to improve its products.
- `CANDIDATE_PROFILE` is a secret and is never published. It's sent to Gemini for fit scores.

## If something goes wrong

- **The run is red in Actions:** click it and open "Find new jobs" to read what happened. Each source reports separately.
- **"What today's search checked"** at the bottom of the Jobs tab shows the same report on your page.
- **Teletalk says it needs a browser:** that page sometimes loads its list with scripts. Government
  circulars still come in through the web searches.
- **Tailoring says a model is unavailable:** Google retires model names from time to time. Put a
  current one in **Settings > Advanced** (see https://ai.google.dev/gemini-api/docs/models), and
  update the `ai.models` list in `config.json`.
- **LinkedIn jobs are missing:** LinkedIn hides most postings from search. Use **Applications > Add a
  job**, paste the post, and press **Fill details from the description**.
