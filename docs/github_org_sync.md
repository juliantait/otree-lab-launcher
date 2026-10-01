# GitHub: setup and use

This guide explains how to let lab PCs download ("clone") experiment code from
your lab's GitHub organisation and keep it up to date, straight from the
launcher, with no terminal.

## The quick path

**Lab manager, once, on GitHub** (section 2):

1. **Create an organisation**, base member permission **Read**.
2. **Make the lab's own GitHub account a member**, and allow fine-grained tokens.

**On each lab PC, once, in the launcher** (sections 3 and 5a):

1. **Gear -> Settings -> GitHub -> Edit. Type the organisation name.**
2. **Click "Add token"**, then **"Create the token on GitHub"** (signed in
   as the lab account): choose *All repositories*, generate, copy.
3. **Pick who you are under "Added by", paste the token, Save.** The launcher
   checks the token with GitHub before it stores it.

**Researcher** (sections 4 and 5):

1. **Create the study's repository in the organisation** and push to it.
2. **In the launcher: GitHub, pick the study, Enter.**
3. Later: when the project box says **A newer version of this study is
   available**, click **Update**.

The rest of this guide is the detail. Where a step happens on GitHub's website,
it links GitHub's own instructions instead of copying their screenshots, because
GitHub changes its pages often.

Throughout, `<your-org>` means your organisation's name, `<study-repo>` the
name of one study's repository, and `<lab-account>` the lab's own GitHub account
that the shared lab PCs use (sections 2.5 and 3a). Replace them with your own.

**Contents**

1. [What an organisation is, and why use one](#1-what-an-organisation-is-and-why-use-one)
2. [Lab manager: set up the organisation](#2-lab-manager-set-up-the-organisation)
3. [Give each computer a GitHub token](#3-give-each-computer-a-github-token)
4. [Researcher: put a study into the organisation](#4-researcher-put-a-study-into-the-organisation)
5. [Using it in the launcher](#5-using-it-in-the-launcher)
6. [Good practice](#6-good-practice)
7. [Moving repositories to another organisation later](#7-moving-repositories-to-another-organisation-later)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. What an organisation is, and why use one

A GitHub **organisation** is a shared account that belongs to the lab rather
than to one person. Repositories ("repos") created under it are **owned by the
lab**, so they stay available when a researcher leaves.

Why the launcher needs one:

- **Access is controlled by organisation roles.** Who can see a study is decided
  by membership of the organisation and the member's role, not by sharing repos
  one person at a time.
- **Lab PCs only need to read.** An organisation lets members have **read**
  access, and lets the lab issue one **read-only token** for its shared PCs:
  they can clone and pull a study but cannot change it on GitHub.
- **Only organisation-owned repos can be shared read-only.** A repo on a
  personal account can only be shared by adding individual collaborators, and
  collaborators on a personal repo always get **write** access
  ([GitHub: personal repo permissions](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-user-account-settings/permission-levels-for-a-personal-account-repository)).
  So every study that lab PCs should pull must live **in the organisation**.

---

## 2. Lab manager: set up the organisation

Do this once, on the GitHub website.

1. **Create the organisation.** The free plan is enough.
   [GitHub: create an organisation](https://docs.github.com/en/organizations/collaborating-with-groups-in-organizations/creating-a-new-organization-from-scratch)
2. **Set the base member permission to Read.** In the organisation:
   *Settings → Member privileges → Base permissions → Read*. Every member can
   then **clone and pull every repo, but not push**. A researcher still gets full
   access to the repos they create themselves (the creator of a repo is its
   admin).
   [GitHub: base permissions](https://docs.github.com/en/organizations/managing-user-access-to-your-organizations-repositories/managing-repository-roles/setting-base-permissions-for-an-organization)
3. **Let members create repos** (so researchers can publish their own studies):
   *Settings → Member privileges → Repository creation*, tick **Private**.
   [GitHub: restrict repo creation](https://docs.github.com/en/organizations/managing-organization-settings/restricting-repository-creation-in-your-organization)
4. **Invite the researchers as members.** Each one uses their own GitHub
   account.
   [GitHub: invite users](https://docs.github.com/en/organizations/managing-membership-in-your-organization/inviting-users-to-join-your-organization)
5. **Make sure the lab's own GitHub account is a member.** The lab has one
   GitHub account of its own, `<lab-account>` (if not, sign one up with a lab
   email address and keep its password with the lab's other admin passwords).
   **Invite it to the organisation as a member.** With base permission Read it
   can read every repo but push nothing. It is used for the shared lab PCs (3a).
   [GitHub: create an account](https://docs.github.com/en/get-started/start-your-journey/creating-an-account-on-github)
6. **Allow fine-grained personal access tokens.** In the organisation:
   *Settings → Third-party Access → Personal access tokens → Settings*, and
   **allow access via fine-grained personal access tokens**. On the same page,
   decide whether an owner must **approve** each new token (safer: you see
   every token that can read lab code; approve it under *Pending requests*).
   [GitHub: token policy](https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/setting-a-personal-access-token-policy-for-your-organization) ·
   [approving requests](https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/managing-requests-for-personal-access-tokens-in-your-organization)

---

## 3. Give each computer a GitHub token

Git needs a stored GitHub login to open the organisation's private repos. It is
set up **once per computer** and stored by the operating system (Windows
Credential Manager, the macOS Keychain). The launcher itself **never stores a
token**: it hands what you paste to that system store and runs git, which uses
what the computer has stored. **No GitHub username is asked for or shown.** For
each token the launcher remembers only **who added it** (a name from the
researcher list), **when**, and the token's **expiry date**; Settings shows it as
*Token, added 1 Oct 2026 by Julian · valid until 30 Sep 2027*. **A computer can
hold several tokens** (3d).

**Git never asks for a login by itself**, on Windows or on a Mac: the launcher
runs git with every prompt switched off, so no sign-in window can pop up in the
middle of a session. With no login stored, a clone or an update says so and
offers **GitHub login…**, which opens the one dialog for it (**Add a GitHub
token**; in Settings the same dialog is **Add token**).

**Which login to use:**

| Computer | Use |
|---|---|
| **Shared lab PC** | **The lab's read-only token (3a).** **Never** a personal account. |
| **A researcher's own computer** | **Your own GitHub account** (3b): a sign-in git already has is used as it is, or a token of your own in the same dialog. |

Why never a personal account on a shared PC: everyone who later uses that PC
would be acting as that person on GitHub, including with their write access to
other repos. **Settings -> GitHub shows who is logged in on this computer**, so
anyone can check.

### 3a. Shared lab PCs: one read-only lab token

A **fine-grained personal access token** is a password that can only do what
you allow. One token, made by the lab account, serves every lab PC.

- **It can only read this organisation's code**, nothing else, and can never
  change anything.
- **It survives staff changes**, because it belongs to the lab account, not to a
  person.
- **No personal login is ever left on a shared PC.**

**Create the token (lab manager, once)**

1. **Sign in to GitHub as `<lab-account>`.**
2. In the launcher: **gear -> Settings -> GitHub -> Add token -> Create the
   token on GitHub.** GitHub's form opens **already filled in**: the
   organisation as *Resource owner*, *Contents: Read-only* (GitHub adds
   *Metadata: Read-only* itself) and the expiry.
3. **Repository access: choose "All repositories".** This also covers study
   repos added later.
4. **Generate the token and copy it.** GitHub shows it only once; keep it with
   the lab's admin passwords. If the organisation requires approval (step 2.6),
   **approve it** in the organisation settings.

(By hand instead: *Settings -> Developer settings -> Personal access tokens ->
Fine-grained tokens -> Generate new token*, with the same choices.
[GitHub: create a fine-grained token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token))

**Store it on each lab PC (once)**

1. **Gear -> Settings -> GitHub -> Add token**
2. **Added by: pick your name** from the list (or type a new one; it joins the
   researcher list). **Paste the token. Save.** (No GitHub username: GitHub
   ignores it for a token.)

The launcher asks GitHub once whether it accepts the token:

| It says | Meaning |
|---|---|
| **Token saved, added by Julian. GitHub accepts it. Valid until 30 Sep 2027.** | Stored. Settings lists *Token, added 1 Oct 2026 by Julian · valid until 30 Sep 2027*. |
| **GitHub did not accept this token (mistyped, expired or revoked). Nothing was saved.** | Paste it again, or make a new one. |
| **Token saved, added by Julian. Not checked: GitHub could not be reached.** | Stored without the check (the PC was offline); no expiry date is shown for it. |
| **This token is already on this computer (added … by …). Nothing new was saved.** | Nothing to do. |

**Renewing.** From **14 days before the expiry date** the launcher shows an
amber notice when it starts (*"The GitHub token on this computer expires on …"*).
On GitHub, as `<lab-account>`, **open the token and click "Regenerate token"**
(same settings, new value and expiry), then on each lab PC **Add token**, paste,
Save, and **Delete** the old one in the list. The start-up notice names the
token (*"The GitHub token added 1 Oct 2026 by Julian expires on …"*).

**Revoke it** (a PC is lost, or the token leaked): **delete the token** on
GitHub under the lab account's *Settings -> Developer settings -> Fine-grained
tokens*, or as an organisation owner under the organisation's *Settings ->
Personal access tokens -> Active tokens*. It stops working immediately; then
issue a new one.
[GitHub: reviewing and revoking tokens](https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/reviewing-and-revoking-personal-access-tokens-in-your-organization)

**Remove a token from a PC:** **Settings -> GitHub**, **Delete** next to that
token. It asks first, naming who added it and when, because studies only that
token can open stop updating on that PC.

### 3b. A researcher's own computer

**If this computer is already signed in to GitHub for git** (you clone or push
with git, use GitHub Desktop, or ran `gh auth login`), **there is nothing to
do**: the launcher's git reuses that sign-in.

Otherwise either:

- **In the launcher:** **Add token**, your name, a token of your own, Save. Or
- **Sign in once outside the launcher**, then use the launcher:
  - **Windows:** install [Git for Windows](https://git-scm.com/download/win)
    (default options include Git Credential Manager), clone any private repo of
    the organisation once in *Git Bash*, and choose **Sign in with your
    browser** in the window that opens.
  - **macOS:** install [GitHub CLI](https://cli.github.com), run
    **`gh auth login`** (GitHub.com, HTTPS, authenticate git: Yes, web
    browser), then **`gh auth setup-git`**.

**Switch account:** **Add token** with the other token, then **Delete** the old
one.

### 3c. Without the launcher's dialog (fallback)

The dialog needs git's credential store, which Git for Windows and the git that
comes with macOS both have. If the launcher says *"git on this computer has no
credential store set up"*, or you prefer a terminal: clone any study of the
organisation once in *Git Bash* / *Terminal*; when git asks, enter
`<lab-account>` as the username (GitHub ignores it for a token, any name works)
and the token as the password; the system stores it. To remove a
stored login by hand: Windows **Control Panel -> Credential Manager -> Windows
Credentials**, remove `git:https://github.com`; macOS **Keychain Access**,
search `github.com`, delete the entry. A login added this way shows in
Settings as *Token, added before <date> by unknown*.

### 3d. Several tokens on one computer

A computer can hold **several tokens**: the lab's read-only token plus, for
example, a researcher's own token for a private repository of their own.
**Settings -> GitHub** lists each one by **who added it and when**; **Add token**
adds one, **Delete** removes one.

**Which token git uses:**

- **Getting a study** tries the tokens in the order they were added (then a
  login stored outside the launcher) and uses the **first one that can open
  it**. That token is remembered in the study folder's link to GitHub, so its
  updates use the same token by themselves.
- **Update / the update check:** if GitHub refuses that token (it was deleted,
  or the folder was cloned before), the launcher tries this computer's other
  tokens for that one update. Nothing in the study folder changes.
- **The study list** in the GitHub dialog shows what **any** of the tokens can
  see.

A login stored **before** this version (one login per computer) keeps working
and shows as one token *"added before <date> by unknown"* until it is deleted.

---

## 4. Researcher: put a study into the organisation

A study reaches the lab PCs only once its repo is **owned by the organisation**.

### 4a. Create an empty repo in the organisation

1. On GitHub, click **+ → New repository**.
2. **Owner: choose `<your-org>`** (not your own account).
3. Name it (this is `<study-repo>`, the name you will type in the launcher).
4. **Visibility: Private.**
5. **Leave it empty**: do not add a README, .gitignore or licence (they would
   clash with the files you push next).

[GitHub: create a repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)

**Keep study repos private.** A public repo also works with the launcher (it
clones with no login at all), but then anyone on the internet can read the code.

### 4b. Push your existing study folder into it

**If the folder is not yet a git repo:** in a terminal, inside your oTree
project folder:

```
git init
git add .
git commit -m "First version of the study"
git branch -M main
git remote add origin https://github.com/<your-org>/<study-repo>.git
git push -u origin main
```

**If the folder is already a git repo** (for example it already lives on your
personal GitHub), **add the organisation as a second remote** and push to it.
Your personal copy stays as it is:

```
git remote add lab https://github.com/<your-org>/<study-repo>.git
git push -u lab main
git push lab --tags
```

From then on, `git push lab` sends new work to the lab copy. (Use your branch
name if it is not `main`.)

Or let every plain `git push` update both copies: see *Bring your study to the
lab with GitHub* in the [user guide](user_guide.md#bring-your-study-to-the-lab-with-github).

[GitHub: add local code to GitHub](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)

**Alternative: Import repository.** On GitHub, **+ → Import repository** copies
a repo from another address (for example another git host) into the
organisation, history included, with no terminal.
[GitHub: GitHub Importer](https://docs.github.com/en/migrations/importing-source-code/using-github-importer/importing-a-repository-with-github-importer)

**Warning: "Transfer ownership" moves, it does not copy.** Transferring your
personal repo to the organisation removes it from your account. That is fine if
you want the organisation to be the only home; if you want to keep your own copy,
use the second-remote route above. See also section 7.

---

## 5. Using it in the launcher

### 5a. Turn it on (the organisation name)

1. Open **gear -> Settings -> GitHub -> Edit**.
2. **Type the organisation name** exactly as it appears in the address
   `https://github.com/<your-org>`. That is the whole setting: **an empty field
   is off**.

It is a lab setting, saved with the rest of the lab setup in
`data/lab_info.json` (keys `github_sync_enabled` and `github_org`), so it
travels with that file: every computer of the lab that uses the same
`lab_info.json` gets it. The tokens do **not** travel (they are per computer):
on a PC that has the organisation but no token, the Settings line reads
*`<your-org>` · no token on this computer* and the section opens itself.

A **GitHub** button now appears next to **Browse**.

### 5b. Get a study (clone)

1. Click **GitHub** (next to Browse).
2. **Pick the study** from the list (the organisation's repositories this
   computer's login can see), or **type its name**. Pasting a full GitHub link
   works too, also for a repository outside the organisation.
3. **Researcher: pick your name** (or type a new one; it joins the researcher
   list). It is required: the computer keeps a log of who got which study (5f).
4. **Press Enter** (or **Clone**).

A study **picked from the list** is cloned straight away: the list already proved this computer's token can read it. A typed name or a pasted link is checked first (at most 20 s).

**Saves to** shows the folder that will be created. It is already filled in:
the folder used last time on this computer, and the first time the launcher's
`local/` folder (a scratch folder git ignores, never touched by updating the
launcher). **Change…** picks another.

The dialog stays open and shows *Checking that `<your-org>/<study-repo>`
exists…*, then *Cloning…*. **On success it closes and the new folder is selected
as your study folder.** If something goes wrong it stays open with the name
still filled in, says **what to do in bold**, and the button becomes **Retry**.
Git's own output sits in a collapsed **Git output** block if you need it.

| The dialog says (bold) | What it means | What to do |
|---|---|---|
| **Check the name, or use another GitHub login.** | No repository of that name, **or** this PC's login has no access to it. **A login without access looks exactly like a missing repo**: GitHub gives the same answer for both, on purpose. | **Check the spelling first** (or pick from the list). If it is right: is the repo owned by the organisation (section 4a)? Is this PC's token for this organisation (sections 2, 3)? **GitHub login…** right there changes the login and retries. |
| **GitHub did not answer in time. If a system window asked for permission (Keychain / Credential Manager), allow it and press Retry.** | The check took more than 20 s. Usually the first GitHub request on a computer: Git Credential Manager starting up on Windows, or a Keychain question on a Mac hidden behind the launcher. | **Look for that window, allow it, press Retry** (it is usually quick the second time). The activity log lists every git step with how long it took. |
| **Add a GitHub login for this computer.** | No login is stored, or it was not accepted (expired or revoked token). | **Click GitHub login…**, add a valid token, **Save and retry**. |
| **Use that folder, or choose another folder.** | The study is already on this computer. | **Click Use that folder**: it is selected, and the update check says whether it is behind. |
| **Could not reach GitHub.** | No network. | **Check the internet connection**, then Retry. |

If the list is empty with *"This computer's login sees no repositories in
`<your-org>`"*, the login works but has no access: the token is for another
organisation, or still waits for owner approval (section 2.6).

### 5c. Is this study up to date?

You do not have to remember to update. **Each time you select a study folder or
a saved config, and again when you press Launch if the last check is more than
ten minutes old**, the launcher quietly checks, in the background, whether the
folder is behind its online copy. It never slows down or blocks choosing a study
or launching.

This works for **every study folder that is a git repository**, whatever its
origin: cloned with the GitHub button, in a terminal, with GitHub Desktop, or
from another git server. It does not need the organisation name.

**What the check does.** It runs `git fetch` and compares the folder with what
is online. **Fetch only downloads what is new into git's own storage; it does
not change your experiment files.** The check can never ask for a login.

**What you see in the project status box:**

| You see | It means | What to do |
| --- | --- | --- |
| **Experiment up to date · checked 14:02** (small, grey) | The folder has everything the online copy has. | Nothing. |
| **A newer version of this study is available. Update?** with an **Update** button (amber) | A newer version exists and updating will work. Edits to *other* files on this computer (for example the lab block that **Get ready for the lab** added to `settings.py`) do not get in the way. | **Click Update.** The line turns into the result: *Pulled N changed files.* |
| ***settings.py* was changed on this computer and in the newer version.** with **Get a fresh copy** | The same file was edited here and online, so the folder cannot simply be updated. (Or: *This folder has changes of its own that are not in the online copy*.) | **Click Get a fresh copy** (5e), or have the researcher put the edits into the study. |
| **Could not check for a newer version: the login was not accepted.** with **GitHub login…** (grey) | This computer's login was rejected: typically the lab token expired. | **Click GitHub login…** and store a valid one. |
| *Nothing at all* | Nothing to say: the folder is not a git repository, has no online branch to compare with, the computer is offline, or the check took too long. | Nothing. No message means no check result, not an error. |

**Before you launch.** If a newer version is waiting, the **Before you launch**
screen shows an amber reminder, **"A newer version of this study is available.
Update before launch."**, with an **Update** button. It is a warning, not a
stop: update there, or launch the version you have.

**The one-click shortcut** asks the same question before it launches a saved
config with a newer version waiting: a box *"… Launch anyway?"*. Offline, not a
git folder or a login problem: no question. (See the user guide.)

### 5d. Update (Git Pull)

The banner's **Update** button and the standing **Git Pull** button in the
project box do the same thing; only one of them is on screen at a time. They
**move the folder forward to the newest version and never do anything else**:
no merge, no change to a file that was edited on this computer. The result
appears in the project status box:

- **Nothing new: already up to date.**
- **Pulled N changed files.** with the date and subject of the newest commit
  that came in, and a collapsed **Changes pulled from Git** list with each file
  marked *added*, *modified* or *deleted*, plus lines added and removed.
  **Check the date and subject are the version you expect** before launching.
- **Git pull failed**, with the reason and, right under it, **the button that
  fixes it**: **GitHub login…** for a login problem, **Get a fresh copy** when
  the folder was changed on this computer. **Nothing was changed** in the folder
  when it fails.

The result stays while that study folder is selected and clears when you choose
a different one.

### 5e. Get a fresh copy

When a file was edited on this computer and also in the newer version, or
someone committed on the lab PC, the folder cannot be moved forward. **The
launcher never commits, pushes, merges or discards anything in a study folder.**
Instead, **Get a fresh copy** downloads the study again into a **new folder next
to the old one**, named `<study-repo>_fresh_<date>`, and selects it. The old
folder stays exactly as it is (delete it yourself once you are sure nothing in
it is needed).

A fresh copy that does not yet carry the lab block gets the usual **Get ready
for the lab** offer. It is added to this computer's clone log (5f) as a fresh
copy, under the researcher who got the old folder.

### 5f. Which studies are on this computer

**Settings -> GitHub -> Studies from `<your-org>` on this computer** lists every
study cloned on this computer (and every fresh copy), newest first:
repository, **researcher**, folder, date. A folder that has since been deleted
or moved is marked *folder no longer exists*. Per row: **Open folder** (in
Explorer / Finder) and **Use as study** (selects it, like Browse).

The log is `data/clone_history.jsonl` on this computer (one line per clone,
never copied to other PCs, never committed). It records which token was used by
its label only, never the token.

---

## 6. Good practice

- **Only ever add to the history; never force-push.** Lab PCs pull from the
  organisation repo. A `git push --force` (or rewriting history) makes their next
  pull fail or mix versions. To undo a change, make a new commit that reverts it
  (`git revert`).
- **Do not edit study files on the lab PC.** If a file on the lab PC has local
  edits and the same file changes on GitHub, the folder cannot be updated: the
  launcher names the file and offers **Get a fresh copy** (5e). Make changes on
  your own computer, push them, then update in the lab.
- **Commit the lab block into the study repo.** The launcher's **Get ready for
  the lab** button adds the lab block to `settings.py`. Done on a lab PC, updates
  keep working as long as the newer version does not change `settings.py` too;
  when it does, the lab PC needs a fresh copy. The clean setup is to **add the
  block once on your own computer and commit it** to the study repo. It is inert
  off the lab (it does nothing unless the launcher's environment variables are
  set), so it is safe everywhere. Also add `*.bak` to the repo's `.gitignore` so
  the backup the button makes is never committed.
- **Tag the version that ran a session**, so you can always tell exactly what
  code produced a dataset:

  ```
  git tag -a session-2026-10-01 -m "Pilot session, lab A"
  git push origin --tags
  ```

  [GitHub / git: tagging](https://git-scm.com/book/en/v2/Git-Basics-Tagging)

---

## 7. Moving repositories to another organisation later

For example, from a test organisation to the lab's permanent one. Use
**Settings → General → Danger Zone → Transfer ownership** on each repo.
[GitHub: transferring a repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/transferring-a-repository)

What carries over, and what you must redo:

- **Kept:** the full history, branches and tags.
- **Redirected:** old web links and existing clones' git remotes keep working,
  because GitHub redirects the old address to the new one.
- **Before transferring:** you need permission to create repos in the target
  organisation, and **the target must not already have a repo with the same
  name**.
- **Not kept:** organisation members, teams and their access. **Set up access in
  the new organisation** (section 2).
- **The lab account and the researchers must be members of the new
  organisation.** Invite `<lab-account>` and every researcher there, with base
  permission Read, and allow fine-grained tokens there too (section 2).
- **A token belongs to one organisation, so make a new one.** The old lab token
  (resource owner = the old organisation) cannot read the new one. **Create a
  new token with the new organisation as resource owner** (3a), **add it on each
  lab PC** (**Add token**) and **Delete** the old one there.
- **Update the launcher:** in **Settings → GitHub**, **change the organisation
  name** (once, in the `lab_info.json` the lab PCs share) and add the new token
  on each lab PC with **Add token**.
- **Never create a new repo with the old name in the old organisation.** That
  breaks the redirect, and old clones would start pulling from the new, unrelated
  repo.
- **Optional, tidy:** point existing clones at the new address directly, inside
  each study folder:

  ```
  git remote set-url origin https://github.com/<new-org>/<study-repo>.git
  ```

---

## 8. Troubleshooting

| Message | Likely cause | Fix |
|---|---|---|
| **Check the name, or use another GitHub login.** (clone) / *GitHub has no such repository …* (update) | Name misspelt, repo not in the organisation, or this PC's login has no access to it: the token is for a different organisation, not approved yet, or the account is not a member (a private repo looks "missing" to a login without access). | Pick the study from the list instead of typing; check the repo's owner is `<your-org>`; check this PC's tokens in **Settings → GitHub**, or add one with **GitHub login…**. |
| **Add a GitHub login for this computer.** / **GitHub did not accept this computer's login** / **Could not check for a newer version: the login was not accepted.** | **The lab token expired or was revoked**, is still waiting for owner approval, or no login is stored. | **GitHub login…** (it is offered right there): add a valid token, then delete the old one in Settings. Lab manager: regenerate the token first (3a). |
| **GitHub did not accept this token (mistyped, expired or revoked). Nothing was saved.** | The token pasted into the token dialog is wrong. | Paste it again; if it still fails, make a new one (**Create the token on GitHub**). |
| **The GitHub token on this computer expires on …** (amber, at start) | The stored token's expiry date is less than 14 days away. | Regenerate it on GitHub and store the new one on each lab PC (3a, Renewing). |
| **Not saved: git on this computer has no credential store set up** | The login dialog found no git credential helper, so git could not remember a login. | Install Git for Windows with its default options (they include Git Credential Manager); on a Mac use the git that comes with macOS or `gh auth setup-git` (3c). |
| ***file* was changed on this computer and in the newer version.** / **Local changes to `<file>` … would be overwritten** | Someone edited that file on the lab PC, and it also changed online. Nothing was changed. | **Get a fresh copy** (5e). If the local edit is needed, commit it to the study repo from your own computer instead (section 6). |
| **This folder has changes of its own that are not in the online copy** | Someone committed on the lab PC. | **Get a fresh copy** (5e); ask whoever did it to push that work from their own machine. |
| **Could not reach GitHub** | No internet on the lab PC, or a proxy or firewall is blocking github.com. | Check the network; ask IT to allow `github.com` over HTTPS. |
| **This folder is not a git repo** | The selected folder was copied, not cloned. | Get it with the **GitHub** button instead. |
| No list of studies in the clone dialog | The launcher could not reach `api.github.com` (only the list needs it). | Type the name; cloning works without the list. |
