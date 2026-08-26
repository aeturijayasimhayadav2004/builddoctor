# The Vite DEV server. Not a production image - see the note in
# docker-compose.yml and the README.
#
# Alpine, because this image only has to run node. There is no compiler
# toolchain needed and nothing here is baked in for offline use, unlike the
# Python image which carries the embedding model weights.
FROM node:24-alpine

WORKDIR /app

# Dependencies first, source second. Docker caches each step, so editing a
# component re-runs only the COPY below - not a fresh npm install.
COPY package.json package-lock.json ./

# `npm install`, not `npm ci`. The lock file is generated on Windows, and
# some dependencies ship a different prebuilt binary per platform; install
# resolves the Linux one, ci can insist on the locked Windows entry and
# fail. Reproducibility matters less here than it would in a production
# image, because this container only ever runs the dev server.
RUN npm install

COPY . .

EXPOSE 5173

# --host makes Vite listen on 0.0.0.0. Without it, it binds to the
# container's own loopback and the published port reaches nothing.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
