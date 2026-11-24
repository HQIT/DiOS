package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"github.com/go-jose/go-jose/v4"
	"testing"
	"time"
)

func TestVerifyLogout(t *testing.T) {
	now := time.Now().Truncate(time.Second)
	rsaKey, _ := rsa.GenerateKey(rand.Reader, 2048)
	ecKey, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	foreign, _ := rsa.GenerateKey(rand.Reader, 2048)
	for _, alg := range []jose.SignatureAlgorithm{jose.RS256, jose.ES256} {
		t.Run(string(alg), func(t *testing.T) {
			var key interface{} = rsaKey
			if alg == jose.ES256 {
				key = ecKey
			}
			private := jose.JSONWebKey{Key: key}
			public := private.Public()
			r := request{Issuer: "https://id.example", ClientID: "portal", JWKS: jose.JSONWebKeySet{Keys: []jose.JSONWebKey{public}}, Algorithms: []string{string(alg)}}
			base := func() map[string]interface{} {
				return map[string]interface{}{"iss": r.Issuer, "aud": r.ClientID, "sub": "alice", "sid": "session-a", "iat": now.Unix(), "exp": now.Add(time.Minute).Unix(), "jti": "logout-one", "events": map[string]interface{}{"http://schemas.openid.net/event/backchannel-logout": map[string]interface{}{}}}
			}
			sign := func(c map[string]interface{}, k interface{}) string {
				signer, err := jose.NewSigner(jose.SigningKey{Algorithm: alg, Key: k}, nil)
				if err != nil {
					t.Fatal(err)
				}
				raw, _ := json.Marshal(c)
				jws, err := signer.Sign(raw)
				if err != nil {
					t.Fatal(err)
				}
				token, err := jws.CompactSerialize()
				if err != nil {
					t.Fatal(err)
				}
				return token
			}
			for _, target := range []string{"both", "sid", "sub"} {
				c := base()
				if target == "sid" {
					delete(c, "sub")
				}
				if target == "sub" {
					delete(c, "sid")
				}
				r.Token = sign(c, key)
				got, err := verify(r, now)
				if err != nil || got.EventID != "logout-one" {
					t.Fatalf("%s: %v", target, err)
				}
			}
			cases := map[string]func(map[string]interface{}){
				"issuer":        func(c map[string]interface{}) { c["iss"] = "https://evil.example" },
				"audience":      func(c map[string]interface{}) { c["aud"] = "other" },
				"nonce":         func(c map[string]interface{}) { c["nonce"] = "not-permitted" },
				"nonce-null":    func(c map[string]interface{}) { c["nonce"] = nil },
				"missing-exp":   func(c map[string]interface{}) { delete(c, "exp") },
				"missing-iat":   func(c map[string]interface{}) { delete(c, "iat") },
				"missing-jti":   func(c map[string]interface{}) { delete(c, "jti") },
				"no-target":     func(c map[string]interface{}) { delete(c, "sid"); delete(c, "sub") },
				"expired":       func(c map[string]interface{}) { c["exp"] = now.Add(-time.Second).Unix() },
				"old":           func(c map[string]interface{}) { c["iat"] = now.Add(-121 * time.Second).Unix() },
				"future":        func(c map[string]interface{}) { c["iat"] = now.Add(31 * time.Second).Unix() },
				"long-life":     func(c map[string]interface{}) { c["exp"] = now.Add(301 * time.Second).Unix() },
				"missing-event": func(c map[string]interface{}) { c["events"] = map[string]interface{}{} },
				"event-null": func(c map[string]interface{}) {
					c["events"] = map[string]interface{}{"http://schemas.openid.net/event/backchannel-logout": nil}
				},
				"event-array": func(c map[string]interface{}) {
					c["events"] = map[string]interface{}{"http://schemas.openid.net/event/backchannel-logout": []string{}}
				},
				"event-nonempty": func(c map[string]interface{}) {
					c["events"] = map[string]interface{}{"http://schemas.openid.net/event/backchannel-logout": map[string]int{"x": 1}}
				},
			}
			for name, mutate := range cases {
				t.Run(name, func(t *testing.T) {
					c := base()
					mutate(c)
					r.Token = sign(c, key)
					if _, err := verify(r, now); err == nil {
						t.Fatal("accepted invalid token")
					}
				})
			}
			raw, _ := json.Marshal(base())
			r.Token = "eyJhbGciOiJub25lIn0." + base64.RawURLEncoding.EncodeToString(raw) + "."
			if _, err := verify(r, now); err == nil {
				t.Fatal("accepted unsigned token")
			}
			if alg == jose.RS256 {
				r.Token = sign(base(), foreign)
				if _, err := verify(r, now); err == nil {
					t.Fatal("accepted foreign signature")
				}
			}
		})
	}
}
