// Local verifier adapter. JWT/OIDC verification is owned by coreos/go-oidc.
package main

import (
	"context"
	"crypto"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/go-jose/go-jose/v4"
)

type request struct {
	Token      string             `json:"token"`
	Issuer     string             `json:"issuer"`
	ClientID   string             `json:"client_id"`
	JWKS       jose.JSONWebKeySet `json:"jwks"`
	Algorithms []string           `json:"algorithms"`
}

type event struct {
	Issuer    string `json:"issuer"`
	ClientID  string `json:"client_id"`
	Subject   string `json:"subject"`
	SessionID string `json:"session_id"`
	EventID   string `json:"event_id"`
	IssuedAt  int64  `json:"issued_at"`
	ExpiresAt int64  `json:"expires_at"`
}

func verify(r request, now time.Time) (*event, error) {
	if len(r.Token) == 0 || len(r.Token) > 16384 || r.Issuer == "" || r.ClientID == "" || len(r.JWKS.Keys) == 0 || len(r.JWKS.Keys) > 32 || len(r.Algorithms) == 0 {
		return nil, errors.New("configuration")
	}
	for _, a := range r.Algorithms {
		if a != "RS256" && a != "ES256" {
			return nil, errors.New("algorithm")
		}
	}
	keys := []crypto.PublicKey{}
	for _, k := range r.JWKS.Keys {
		if k.Use != "" && k.Use != "sig" {
			continue
		}
		if k.Algorithm != "" {
			allowed := false
			for _, a := range r.Algorithms {
				if k.Algorithm == a {
					allowed = true
				}
			}
			if !allowed {
				continue
			}
		}
		if !k.IsPublic() || !k.Valid() {
			return nil, errors.New("public key")
		}
		keys = append(keys, k.Key)
	}
	if len(keys) == 0 {
		return nil, errors.New("signing key")
	}
	v := oidc.NewVerifier(r.Issuer, &oidc.StaticKeySet{PublicKeys: keys}, &oidc.Config{ClientID: r.ClientID, SupportedSigningAlgs: r.Algorithms, Now: func() time.Time { return now }})
	t, err := v.VerifyLogout(context.Background(), r.Token)
	if err != nil {
		return nil, err
	}
	// Deployment acceptance profile: short-lived events with an empty event object.
	if t.IssuedAt.IsZero() || t.IssuedAt.After(now.Add(30*time.Second)) || t.IssuedAt.Before(now.Add(-120*time.Second)) || !t.Expiry.After(t.IssuedAt) || t.Expiry.Sub(t.IssuedAt) > 5*time.Minute {
		return nil, errors.New("event time")
	}
	var claims struct {
		Events map[string]json.RawMessage `json:"events"`
	}
	if err = t.Claims(&claims); err != nil {
		return nil, err
	}
	var value map[string]json.RawMessage
	if err = json.Unmarshal(claims.Events["http://schemas.openid.net/event/backchannel-logout"], &value); err != nil || value == nil || len(value) != 0 {
		return nil, errors.New("event value")
	}
	for _, s := range []string{t.Issuer, r.ClientID, t.Subject, t.SessionID} {
		if len(s) > 160 {
			return nil, errors.New("claim length")
		}
	}
	if len(t.TokenID) > 1024 {
		return nil, errors.New("event id length")
	}
	return &event{t.Issuer, r.ClientID, t.Subject, t.SessionID, t.TokenID, t.IssuedAt.Unix(), t.Expiry.Unix()}, nil
}

func main() {
	raw, err := io.ReadAll(io.LimitReader(os.Stdin, 131073))
	if err != nil || len(raw) > 131072 {
		fmt.Println(`{"error":"LOGOUT_TOKEN_INVALID"}`)
		os.Exit(2)
	}
	var r request
	if json.Unmarshal(raw, &r) != nil {
		fmt.Println(`{"error":"LOGOUT_TOKEN_INVALID"}`)
		os.Exit(2)
	}
	value, err := verify(r, time.Now())
	if err != nil {
		fmt.Println(`{"error":"LOGOUT_TOKEN_INVALID"}`)
		os.Exit(2)
	}
	_ = json.NewEncoder(os.Stdout).Encode(value)
}
