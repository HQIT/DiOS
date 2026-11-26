FROM sha256:bf02aa31cbbc82abe0aa534d4cb1be22ea108bd3afd9087dfac4deb691be30d0 AS npm
FROM dios-dsh:governed-20261126
USER root
COPY --from=npm /usr/local/lib/node_modules/npm /opt/npm
USER 65534:65534
