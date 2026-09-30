# GitHub Organisation Sync: setup and use

This guide explains how to let lab PCs download ("clone") and update ("pull")
experiment code from your lab's GitHub organisation, straight from the launcher,
with no terminal.

It is written for a **lab manager** (sections 2 and 3, done once) and for
**researchers** (section 3 for their own computer, then sections 4 to 6). You do not need much git experience. Where a
step happens on GitHub's website, this guide links GitHub's own instructions
instead of copying their screenshots, because GitHub changes its pages often.

Throughout, `<your-org>` means your organisation's name, `<study-repo>` the
name of one study's repository, and `<lab-account>` the lab's own GitHub account
that the shared lab PCs use (sections 2.5 and 3a). Replace them with your own.

**Contents**

1. [What an organisation is, and why use one](#1-what-an-organisation-is-and-why-use-one)
2. [Lab manager: set up the organisation](#2-lab-manager-set-up-the-organisation)
3. [Give each computer access to GitHub](#3-give-each-computer-access-to-github)
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

## 3. Give each computer access to GitHub

Git needs a stored GitHub login to open the organisation's private repos. It is
set up **once per computer** and stored by the operating system. The launcher
itself **never stores or sees any password, token or login**; it just runs git,
which uses what the computer has stored.

**Which option to use:**

| Computer | Use |
|---|---|
| **Shared lab PC** | **Recommended: the lab's read-only token (3a).** Simpler alternative: sign the lab account in (3d). **Never** sign in with a personal account. |
| **A researcher's own computer** | **Sign in with your own GitHub account** (3b Windows, 3c macOS). |

Why never a personal account on a shared PC: everyone who later uses that PC
would be acting as that person on GitHub, including with their write access to
other repos.

### 3a. Shared lab PCs (recommended): one read-only lab token

A **fine-grained personal access token** is a password that can only do what
you allow. One token, made by the lab account, serves every lab PC.

Why this is the recommended setup:

- **It can only read this organisation's code**, nothing else, and can never
  change anything.
- **It survives staff changes**, because it belongs to the lab account, not to a
  person.
- **No personal login is ever left on a shared PC.**

**Lab manager, once: create the token**

1. **Sign in to GitHub as `<lab-account>`** and go to **Settings → Developer
   settings → Personal access tokens → Fine-grained tokens → Generate new
   token**.
   [GitHub: create a fine-grained token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token)
2. **Resource owner: `<your-org>`** (not the lab account itself).
3. **Expiration:** set a date (up to a year is typical) and **put a renewal
   reminder in the lab calendar**. When it expires, cloning and pulling stop
   working on every lab PC until you renew it.
4. **Repository access: All repositories.** This also covers study repos added
   later, so you never have to edit the token.
5. **Permissions → Repository permissions → Contents: Read-only.** Nothing else.
   (GitHub adds *Metadata: Read-only* automatically; that is expected.)
6. **Generate the token and copy it.** GitHub shows it only once; keep it with
   the lab's admin passwords. If the organisation requires approval (step 2.6),
   **approve it** in the organisation settings.

**On each lab PC, once: store the token**

1. **Open a terminal**: *Git Bash* on Windows, *Terminal* on macOS.
2. **Clone any study from the organisation once:**

   ```
   cd ~/Desktop
   git clone https://github.com/<your-org>/<study-repo>.git
   ```

3. **When asked to log in, enter `<lab-account>` as the username and paste the
   token as the password.**
   - **Windows:** Git Credential Manager opens a sign-in window; **choose
     "Token"** and paste it. It is stored in **Windows Credential Manager**.
   - **macOS:** type the username at `Username`; at `Password` paste the token
     (nothing appears while you paste) and press Enter. It is stored in the
     **macOS Keychain**.
4. **Delete the test folder.** From now on the launcher's **GitHub** and **Git
   Pull** buttons work without asking.

**Renew or replace the token**

1. On GitHub, as `<lab-account>`, **open the token and click "Regenerate
   token"** (same settings, new value and expiry), or make a new one as above.
2. On each lab PC, **remove the old stored login**:
   - Windows: **Control Panel → Credential Manager → Windows Credentials**,
     remove `git:https://github.com`.
   - macOS: **Keychain Access**, search `github.com`, delete the entry.
3. **Repeat "store the token"** above with the new token.

**Revoke it** (a PC is lost, or the token leaked): **delete the token** on
GitHub under the lab account's *Settings → Developer settings → Fine-grained
tokens*, or as an organisation owner under the organisation's *Settings →
Personal access tokens → Active tokens*. It stops working immediately; then
issue a new one.
[GitHub: reviewing and revoking tokens](https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/reviewing-and-revoking-personal-access-tokens-in-your-organization)

### 3b. Windows: sign in with a GitHub account

Git for Windows ships with **Git Credential Manager**, which handles the
sign-in for you.

1. **Install Git for Windows** from [git-scm.com](https://git-scm.com/download/win)
   with the default options (they include Git Credential Manager).
2. **Clone any private repo from the organisation once**, in *Git Bash*:

   ```
   cd ~/Desktop
   git clone https://github.com/<your-org>/<study-repo>.git
   ```

3. **A GitHub sign-in window opens. Choose "Sign in with your browser"** and
   log in with your account. The login is saved in **Windows Credential
   Manager**.
4. **Delete the test folder.** From now on the launcher's **GitHub** and **Git
   Pull** buttons work without asking.

**Sign out or switch account:** open **Control Panel → Credential Manager →
Windows Credentials**, find `git:https://github.com` and **Remove** it. The next
clone opens the sign-in window again, so you can log in as a different account.

[Git Credential Manager](https://github.com/git-ecosystem/git-credential-manager)

### 3c. macOS: sign in with a GitHub account

**If this Mac is already signed in to GitHub for git** (you already clone or
push with git, or use GitHub Desktop), **there is nothing to do**: the launcher
reuses that sign-in, with nothing new to type (confirmed on macOS).

Otherwise, sign in once with GitHub's command-line tool, `gh`. (The git that
comes with macOS asks for a password in the terminal, and **GitHub no longer
accepts account passwords for git**.)

1. **Install GitHub CLI** from [cli.github.com](https://cli.github.com) (or
   `brew install gh` if you use Homebrew).
2. In *Terminal*, run **`gh auth login`** and answer:
   - *Where do you use GitHub?* **GitHub.com**
   - *Preferred protocol for Git operations?* **HTTPS**
   - *Authenticate Git with your GitHub credentials?* **Yes**
   - *How would you like to authenticate?* **Login with a web browser**. Copy
     the one-time code shown, press Enter, and **sign in in the browser**.
3. Run **`gh auth setup-git`** so git always uses this login.

Signing in to [GitHub Desktop](https://desktop.github.com) works too; the
launcher reuses that sign-in as well.

**Sign out or switch account:** run **`gh auth logout`**, then `gh auth login`
again as the other account. (If git still uses an old login, open **Keychain
Access**, search `github.com` and delete the old entry.)

[GitHub CLI: gh auth login](https://cli.github.com/manual/gh_auth_login) ·
[gh auth setup-git](https://cli.github.com/manual/gh_auth_setup-git)

### 3d. Shared lab PCs, simpler alternative: sign the lab account in

Instead of the token, you can **sign each lab PC in as `<lab-account>`** using
3b (Windows) or 3c (macOS). It is quicker to set up, but the PC then holds a full
login to the lab account rather than a key that can only read code, so the token
(3a) is preferred.

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

### 5a. Turn it on (once per lab PC)

1. Open **gear → Lab Settings → GitHub Organisation Sync**.
2. **Tick "Enable GitHub Organisation Sync".**
3. **Type the organisation name** exactly as it appears in the address
   `https://github.com/<your-org>`.

Both are lab settings, saved with the rest of the lab setup in
`data/lab_info.json` (keys `github_sync_enabled` and `github_org`), so every
launcher using that `data/` folder shares them. A **GitHub** button now appears next to
**Browse**, and a **Git Pull** button appears in the project status box once a
study folder is selected.

### 5b. Clone a study

1. Click **GitHub** (next to Browse).
2. **Type the repository name** (`<study-repo>`, without the organisation).
3. **Choose the parent folder.** The study is cloned into a new
   `<study-repo>` folder inside it. The folder picker opens in the launcher's
   `local/` folder: a scratch folder git ignores, so a study kept there never
   gets committed and is never touched by updating the launcher. Pick any
   other folder if you prefer.
4. **Click Clone.**

The dialog stays open and shows *Checking that `<your-org>/<study-repo>`
exists…*, then *Cloning…*. **On success it closes and the new folder is selected
as your study folder.** If something goes wrong, the dialog stays open with the
name still filled in, shows the reason in bold, and the button becomes **Retry**.
Git's own output sits in a collapsed **Git output** block if you need it.

| The dialog says | What it means | What to do |
|---|---|---|
| **No repository called X found in `<your-org>`, or this computer's GitHub login cannot see it. Check the name.** | Either the name is wrong, **or** the repo is private and this PC's login (the lab token, or the signed-in account) cannot see it. GitHub gives the same answer for both, on purpose, so it does not reveal which private repos exist. | **Check the spelling first.** If it is right: is the repo owned by the organisation (section 4a)? Is this PC's token for this organisation, or is the signed-in account a member (sections 2, 3)? |
| **Could not open … GitHub asked for a login this computer does not have** | No login stored on this PC, the lab token expired or was revoked, or the wrong account is signed in. | **Check the name, then ask the lab manager** to store a valid token or sign the PC in (section 3). |
| **A folder named X already exists in …** | You cloned this study here before. | **Pick another parent folder**, or use the existing folder with Browse and Git Pull. |
| **Could not reach GitHub.** | No network. | **Check the internet connection**, then Retry. |
| **Set the GitHub organisation name … first** | The organisation name is empty. | **Fill it in** (section 5a). |

### 5c. Update a study with Git Pull

With the study folder selected, **click Git Pull**. A result appears at the
bottom of the project status box:

- **Nothing new: already up to date.** The folder already has the latest version.
- **Pulled N changed files.** It also shows the date and subject of the newest
  commit that came in, and a collapsed **Changes pulled from Git** list with each
  file marked *added*, *modified* or *deleted*, plus lines added and removed.
  **Check the date and subject are the version you expect** before launching.
- **Git pull failed**, with the reason (see [Troubleshooting](#8-troubleshooting))
  and git's raw output collapsed underneath. **Nothing was changed** in the
  folder when it fails.

The result stays while that study folder is selected and clears when you choose
a different one.

---

## 6. Good practice

- **Only ever add to the history; never force-push.** Lab PCs pull from the
  organisation repo. A `git push --force` (or rewriting history) makes their next
  pull fail or mix versions. To undo a change, make a new commit that reverts it
  (`git revert`).
- **Do not edit study files on the lab PC.** If a file on the lab PC has local
  edits and the same file changes on GitHub, **Git Pull refuses** ("local changes
  … would be overwritten") and names the file. Make changes on your own computer,
  push them, then Git Pull in the lab.
- **Commit the lab block into the study repo.** The launcher's **Add block to
  settings.py** button edits `settings.py`. Done on a lab PC, that is exactly the
  kind of local edit that later blocks Git Pull. The clean setup is to **add the
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
  new token with the new organisation as resource owner** (3a) and **replace the
  stored token on each lab PC** ("Renew or replace the token" in 3a). Lab PCs
  signed in as the lab account (3d) need nothing new once it is a member.
- **Update the launcher:** in **Lab Settings → GitHub Organisation Sync**, **change
  the organisation name** on each lab PC.
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
| **No repository called X found …** (clone) / *GitHub has no such repository …* (pull) | Name misspelt, repo not in the organisation, or this PC's login cannot see it: the token is for a different organisation, or the signed-in account is not a member (a private repo looks "missing" to a login that cannot see it). | Check the name; check the repo's owner is `<your-org>`; check this PC's token or account (section 3). |
| **GitHub did not accept this computer's login** / **asked for a login this computer does not have** | **The lab token expired or was revoked**, is still waiting for owner approval, or no login is stored; on a personal machine, no account or the wrong account is signed in, or that account is not a member. | Lab manager: approve, regenerate or replace the token and store it again ("Renew or replace the token" in 3a); or sign in with the right account (3b, 3c). |
| **Local changes to `<file>` … would be overwritten** | Someone edited that file on the lab PC, and it also changed on GitHub. Nothing was pulled. | If the local edit is not needed, discard it (in the study folder: `git restore <file>`) and Git Pull again. If it is needed, commit it to the study repo from your own computer instead (section 6). |
| **Could not reach GitHub** | No internet on the lab PC, or a proxy or firewall is blocking github.com. | Check the network; ask IT to allow `github.com` over HTTPS. |
| **This folder is not a git repo** | The selected folder was copied, not cloned. | Clone it with the **GitHub** button instead. |
| **This folder and GitHub both have new commits** | Someone committed on the lab PC. | Ask whoever did it to push that work from their own machine; on the lab PC, re-clone the study into a fresh folder. |
