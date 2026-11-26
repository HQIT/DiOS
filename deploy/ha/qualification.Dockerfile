FROM ubuntu@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends pacemaker-cli-utils resource-agents-base fence-agents-virsh python3 && rm -rf /var/lib/apt/lists/*
