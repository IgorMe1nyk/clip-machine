# Word lists for captions, hooks and post captions (draft v1, Igor to approve)

Platforms (TikTok, Instagram, YouTube) do **not** publish banned-word lists. These lists are risk reduction based on their public policies (adult content, drugs, self-harm, hate, violence) and common creator practice, **not a guarantee** [judgment]. Review them, add or remove words, then they go into `config/wordlists/` as part of Brief 11.1.

## Tier 1: MASK (shown as first letter + ***, e.g. f***) in subtitles, hooks and post captions
Inflections are matched too: -s, -es, -ed, -ing, -er, -ers, -in.

### Profanity
fuck, motherfucker, shit, bullshit, bitch, asshole, dick, pussy, cunt, bastard, cock, dickhead, prick, whore, slut, hoe, thot

### Sexual / adult
porn, porno, pornhub, onlyfans, sex, sexual, sexy, nude, nudes, naked, horny, orgasm, blowjob, hooker, stripper, escort

### Hard drugs
cocaine, coke (see note), crack (see note), meth, heroin, fentanyl, molly, ecstasy, lsd, ketamine, xanax, percocet, oxy, lean, shrooms

### Self-harm
suicide, suicidal, "kill myself", "kill yourself", kys, overdose, cutting (see note)

### Slurs
Always masked AND flagged. Claude Code fills this list from the slurs the platforms' hate-speech policies name (racial, ethnic, homophobic, transphobic, ableist). It's kept in the file only, never printed in chat, logs or commits messages.

## Tier 2: FLAG ONLY (warning in the report; you decide), never auto-masked
These have normal meanings in sports and podcast talk, so masking them would wreck clips ("he k*** that shot").
- **Violence:** kill, killed, killer, murder, shoot, shot, gun, guns, weapon, stab, dead body, bomb, terrorist
- **Gambling:** gambling, casino, stake, betting, bet, slots, roulette
- **Alcohol/weed:** weed, drunk, high, alcohol, vodka, blunt
- **Crime:** scam, fraud, stolen, robbery
- **Self-harm:** "self harm", self-harm (not masked: masking showed it as s\*\*\* -h\*\*\*, which reads like a swear word)

## Tier 3: NEVER touch (normal language)
death, dead, died, die, jail, prison, arrested, fake, faked, hell, damn, crap, sucks, ass (as in "kick ass" / "my ass" stays; only "asshole" is masked)

## Notes
- **coke / crack / cutting** have innocent meanings (Coca-Cola, "crack a joke", "cutting the clip"). They're masked only when next to drug or harm words (e.g. "line of coke", "smoking crack"). Otherwise they're flagged. "self harm" is flag-only now, so it no longer makes "cutting" masked.
- **Per campaign:** `allowed:` (e.g. a show called "High Stakes") and `extra_masked:` in campaign.yaml.
- The **audio is never changed.** Campaigns require the original podcast audio, so a spoken swear stays audible; only the text is masked.
