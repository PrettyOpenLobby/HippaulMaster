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

ENV POL_TITLES=tmtitle
