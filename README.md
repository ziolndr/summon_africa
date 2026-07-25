# SUMMON AFRICA — Production Field V6

V6 removes the eleven-record seed from the production path.

## Production invariant

The ingestion, field build, Docker build, and server all reject a catalog below 500 playable records by default.

## Build and push

Place these files in the cloned `ziolndr/summon_africa` repository, then run:

```bash
chmod +x BUILD_AND_PUSH_PRODUCTION.command
./BUILD_AND_PUSH_PRODUCTION.command
```

Optional controls:

```bash
export YOUTUBE_API_KEY="..."
export SUMMON_AFRICA_PER_CHANNEL=100
export SUMMON_AFRICA_MAX_RECORDS=3000
export SUMMON_AFRICA_MIN_RECORDS=500
./BUILD_AND_PUSH_PRODUCTION.command
```

The command ingests creator-owned playable works, hydrates TMDB art, builds the 72D field, validates the manifest count, commits generated field artifacts, and pushes `main`.
