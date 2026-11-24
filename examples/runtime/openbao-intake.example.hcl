# API admission role: first version only; native KV CAS=0 is also required.
path "dios-sources/data/admission/*" {
  capabilities = ["create", "read"]
}
# No update/delete/list/metadata/undelete/destroy permissions.
# The execution-side reader role should grant only read for the same data prefix.
