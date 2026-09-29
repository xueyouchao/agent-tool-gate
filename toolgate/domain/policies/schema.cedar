namespace toolgate {
  entity Agent = { name: String, kind: String, owner: String, trust: String };
  entity User = { id: String };
  entity Workspace = { root: String };
  entity Path in [Workspace] = { zone: String, scope: String };
  entity Url = { allowlisted: Bool, zone: String, scope: String };
  entity Database in [Workspace] = { scope: String, zone: String };

  action execute appliesTo {
    principal: [Agent],
    resource: [Path, Url, Database],
    context: {
      tool: String,
      subcommand: String,
      command: String,
      reads_secret_path: Bool,
      is_destructive: Bool,
      is_recoverable: Bool,
      intent_match: Bool,
      injected: Bool,
      secret_exposure: Bool,
      egress: Bool,
      spends_money: Bool,
      scope_drift: Bool,
      well_formed: Bool,
      blast_radius: String,
      confidence_floor: Long
    }
  };
}
