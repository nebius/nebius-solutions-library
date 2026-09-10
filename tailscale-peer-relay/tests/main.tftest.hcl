run "test_mode_tailscale_peer_relay_apply" {
  command = apply

  variables {
    test_mode = true
  }
}
