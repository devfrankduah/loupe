# Security policy

Report a security problem privately through GitHub: [open a private report](https://github.com/devfrankduah/loupe/security/advisories/new). Please don't open a public issue for it, and don't paste a Bright Data token or Browser API endpoint into the report, even an expired one.

loupe has no tagged releases yet, so reports apply to the `main` branch.

## In scope

- Endpoint redaction. `BRIGHTDATA_BROWSER_WSS` carries a Browser API password. Any way the endpoint, or a piece of it (the host, username, zone, password or query), ends up in the terminal, an error message, a log or a results file is a vulnerability. The same goes for the API token.
- Terminal escaping. Page text in the terminal summary has its control and direction-changing characters shown as escapes. A page that gets an escape sequence or a bidirectional override through to your terminal is in scope.
- Hostile pages tampering with the checks. The rendered checks run in an isolated JavaScript world so a page's scripts can't patch the browser functions they rely on. A page that makes hidden text look visible to loupe, stops or misleads the inline-style trace, or reaches into the isolated world is in scope.
- The report page. `loupe report` escapes all page text into HTML and sets a Content-Security-Policy that allows no scripts. Text from a scanned page that runs as script or breaks out of its element on the report page is in scope.

## Out of scope

- Hidden text loupe misses, or text it flags that people can see. Those are bugs in the checks, so use the [false positive or missed hidden text](https://github.com/devfrankduah/loupe/issues/new?template=hidden-text.yml) form.
- Problems in Bright Data's services, Playwright or Chromium themselves. Report those to their maintainers.
