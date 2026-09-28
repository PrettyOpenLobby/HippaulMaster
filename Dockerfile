# The Tetra Master title, layered on the OpenLobby core image.
#
# Tetra Master has no server port of its own: the game rides the auth band
# the client already holds and the lobby band's resource fetches, so the
# title runs INSIDE the core's login and authsess processes as a plugin
# (POL_TITLES, see services/titles.py in OpenLobby). This image is the core
# image plus the title package; docker-compose.yml swaps it in for those two
# services. Build the core first (`docker compose up -d --build` in the
# OpenLobby checkout), or point OPENLOBBY_IMAGE at the image you use.
ARG OPENLOBBY_IMAGE=openlobby:latest
FROM ${OPENLOBBY_IMAGE}

# the title package beside the core modules, its reply templates, and the
# tools the weekly ranking job runs
COPY services/ /app/
COPY config/polpro.json /app/polpro.json
COPY tools/ /app/tools/

# services/ lands on top of the core's modules, so refuse an image in which
# a stray copy has replaced the core's live_sessions.py (.dockerignore keeps
# one out of the build context)
RUN python -c "import sys, live_sessions; hasattr(live_sessions, 'marker_key') or sys.exit('live_sessions.py is not the one from the OpenLobby image')"

ENV POL_TITLES=tmtitle
