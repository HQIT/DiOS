# Replace /srv/dios with the installation root and use the service account UID.
vault {
  address = "https://secrets.example"
  ca_cert = "/srv/dios/config/secrets-ca.pem"
}
auto_auth {
  method "approle" {
    config = {
      role_id_file_path = "/srv/dios/secrets/role-id"
      secret_id_file_path = "/srv/dios/secrets/secret-id"
      # Private, narrowly scoped bootstrap, provisioned/rotated by operations.
      # Its lifetime must cover the documented unattended restart window.
      remove_secret_id_file_after_reading = false
    }
  }
  sink "file" {
    config = {
      path = "/srv/dios/secrets/openbao-token"
      mode = 384
    }
  }
}
template_config {
  exit_on_retry_failure = true
  static_secret_render_interval = "5m"
}
template {
  contents = "{{- with secret \"secret/data/model/cursor?version=1\" -}}{{ .Data.data.value }}{{- end -}}"
  destination = "/srv/dios/secrets/model-cursor-v1"
  # Parent is 0700; only this file is mounted read-only into the Agent.
  # Use 0600 instead when Worker and container share the numeric UID.
  perms = "0444"
  backup = false
  error_on_missing_key = true
}
