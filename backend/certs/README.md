# Local TLS interception certificates

Some machines run antivirus or corporate software that terminates TLS and
re-signs it with a private root CA. That CA lives in the host trust store, so
the host works fine, but a Docker build has its own store and every `pip`
download fails certificate verification.

Drop any such root CA here as a `.crt` file (PEM). The image installs whatever
it finds and rebuilds the trust bundle. On a machine without interception the
directory is empty and nothing changes.

Export one on Windows with:

    Get-ChildItem Cert:\LocalMachine\Root |
      Where-Object { $_.Subject -like "*<issuer name>*" } |
      ForEach-Object {
        $b64 = [Convert]::ToBase64String($_.RawData, 'InsertLineBreaks')
        "-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----"
      } | Set-Content backend/certs/local-root.crt -Encoding ascii

These files are local to one machine, so they are not committed.
