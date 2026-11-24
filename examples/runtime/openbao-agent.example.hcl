# OpenBao native Agent: supply role-id and wrapped/provisioned SecretID privately.
vault {
  address = "https://secrets.example"
  ca_cert = "/etc/dios/secrets-ca.pem"
}
auto_auth {
  method "approle" {
    config = {
      role_id_file_path = "/run/dios-secrets/role-id"
      secret_id_file_path = "/run/dios-secrets/secret-id"
    }
  }
  sink "file" {
    config = {
      path = "/run/dios-secrets/openbao-token"
      mode = 384 # 0600
    }
  }
}
