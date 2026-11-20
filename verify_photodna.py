"""One-shot smoke test for the PhotoDNA Cloud Service integration.

Run this (manually, from the venv) once PHOTODNA_API_KEY is in .env to verify:
  1. The API key works against the live MatchHash endpoint.
  2. csam_screening's response parsing matches the real response envelope
     (the envelope was unverified when the adapter was written -- see the
     PhotoDNA constants block in csam_screening.py).
  3. (optionally, with --image) the EdgeHash SDK is wired up end-to-end.

By default it sends Microsoft's own "Test" hash from the onboarding email --
a safe, known hash that the service should report as a match with source
"Test". No imagery is ever sent; this only performs a read-only hash lookup
against Microsoft's API.

Usage:
    source venv/bin/activate
    python verify_photodna.py                 # sends the known Test hash
    python verify_photodna.py --image pic.jpg # hashes pic.jpg via the SDK first
"""
import argparse
import json
import sys

from dotenv import load_dotenv

load_dotenv()  # must happen before importing csam_screening (it reads env at import)

import requests  # noqa: E402

import csam_screening  # noqa: E402
import photodna_hash  # noqa: E402

# The quick-start test hash from the PhotoDNA onboarding email. The cloud
# service recognizes it and returns a match attributed to the hash source
# "Test" -- safe to send, proves the key + endpoint + request shape work.
TEST_HASH = (
    "UEROQQABAgAIT58oAAAAAAAAAADgAAAA4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAw4AAAAA"
    "AAAAAAAAxv8F/3Gc/1j2zEqsPFD/JND/VcwjHf8jT8wGs88EjrgcfYYRDP8HSYUrtrj/OLwh"
    "VoYjOz6ty/9m8f//MsaXsjnFI/+SK+JEB/8DTtQiGrJXOvEMXNEdI6go/wg/X1arzlw6cPAO"
    "YP8B/54D8iYAhCc4jvJiRnEeJ4YjLKoWuJZ0///UlGlfY+6Hwf8IiEUL7rQR2ZO5KmH2XP+G"
    "ywqczXp+LhT0zyz/Y+2+UUo+761nf7X1xdAo0eD6+U3k9n3LD3Hzw/zxDOt+JFAz/vNhlSz3"
    "Ru8Bv8w2zgo6/+4ifLHipIWjk82qg4z48qv+68vrUzyWM/ufh40q+sb1vCPSy5ZYYvshtwG/"
    "+S9ZCRTmC5sl/f7zCoxb6hbVAaDgS01ASP8+gnbV4PpYAyXnX7b9iv6HKNwD5zGQ+irlGHX2"
    "evgHNW/6/ufmIEfWYmCTSP832BMO/Z1vnCPs0psZHft7LxZb7njklCrqO+Yi9tEFJPyT+v7a"
    "T+XrdJQVUvu1ILOD9vQarQvxuWlNaPtLpfPD+ApA+ErxzNdvae8LW2Ak9qLMU+nbrVCt+fNz"
    "iGN540wXtjXahAZJxKBlFYDX+s+4pIvnuzrrSe+RHVxj7APBDK776bhXH+cisbj2+BOw2ZfD"
    "D1o8wfRHHXih/2MOyYraBNfgGfl9osJy+67EtejZnvbwMP+EAkW27bpWddLpS1uWmfed0V3e"
    "+ynR/C/pUsrAlPEGcs2v8mDBsiTkGku1PP3+zMyJ8qgRFrHrp/DJRPwzDF7T+uDoz7Tn8fti"
    "TfpkFfwmva6Rd/r/QWWMCPowEcR27NruceT2wl4fzPJhgZ9M4iLEoOL102+jDfm0712f6eTE"
    "FA7p2sPEIv3JUxm78i1TR9vO5BRjiv+B2mkTxbfB9xnngvKSCP+qBqpa6IDzP57+Oj3s6//W"
    "I6Cb9FAHvvT8b4eMW/XJ0BCX7Ax+ZE7jZgjywuwJOrqs9oH6nJv1uBnkxvsbOtYH+gr8nkzw"
    "Ff8Odvsbj6zy984VzLnq4iyhHP1OeUll8F6mWxbsY9MVUOuZIYug0eyR1pqG5dsbqN1KDpdx"
    "7x2ac/H0MeBDavJpSoM18SkWB7TlFWm+Sutwf1+C5Y4cQarleEWGqf8qzqxS7uRNKn73GWHi"
    "1P5zfvT4"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        help="hash this image with the EdgeHash SDK and match it, instead of "
        "sending the built-in Test hash",
    )
    args = parser.parse_args()

    if not csam_screening.PHOTODNA_API_KEY:
        print("FAIL: PHOTODNA_API_KEY is not set (add it to .env first)")
        return 1

    if args.image:
        if not photodna_hash.is_sdk_configured():
            print(
                "FAIL: EdgeHash SDK not configured -- set PHOTODNA_SDK_DIR to the "
                "extracted PhotoDNA.EdgeHashGeneration SDK (docs/PHOTODNA_SETUP.md)"
            )
            return 1
        with open(args.image, "rb") as f:
            image_bytes = f.read()
        print(f"Generating edge hash for {args.image} ...")
        try:
            edge_hash = photodna_hash.generate_edge_hash(image_bytes)
        except photodna_hash.PhotoDNAHashError as e:
            print(f"FAIL: edge hash generation failed: {e}")
            return 1
        print(f"  edge hash ({len(edge_hash)} chars): {edge_hash[:40]}...")
        expectation = "a match ONLY if the image is in the sample/known set"
    else:
        edge_hash = TEST_HASH
        expectation = 'a match with hash source "Test"'

    print(f"POST {csam_screening.PHOTODNA_MATCH_URL}")
    print(f"  (expecting {expectation})")

    # Raw request first, so the full untouched envelope is visible -- this is
    # what confirms/refutes the response-shape assumptions in csam_screening.
    resp = requests.post(
        csam_screening.PHOTODNA_MATCH_URL,
        headers={
            "Ocp-Apim-Subscription-Key": csam_screening.PHOTODNA_API_KEY,
            "Content-Type": "application/json",
        },
        json=[{
            "DataRepresentation": csam_screening.PHOTODNA_DATA_REPRESENTATION,
            "Value": edge_hash,
        }],
        timeout=csam_screening.PHOTODNA_REQUEST_TIMEOUT_SECONDS,
    )
    print(f"HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        print("FAIL: non-JSON response body:")
        print(resp.text[:2000])
        return 1
    print("Raw response envelope:")
    print(json.dumps(payload, indent=2)[:4000])
    if resp.status_code >= 400:
        print("FAIL: HTTP error from MatchHash (check the API key)")
        return 1

    # Now run the exact production parsing path.
    try:
        results = csam_screening._photodna_match_results(payload)
    except ValueError as e:
        print(f"FAIL: csam_screening could not parse this envelope ({e}).")
        print("      Update _photodna_match_results to accept the raw shape above.")
        return 1

    print(f"Parsed {len(results)} result(s) via csam_screening._photodna_match_results:")
    ok = True
    for i, result in enumerate(results):
        status_ok = csam_screening._photodna_result_status_ok(result)
        is_match = bool(result.get("IsMatch"))
        print(f"  [{i}] status_ok={status_ok} IsMatch={is_match}")
        if not status_ok:
            ok = False
    if not ok:
        print("FAIL: a result reported a non-OK status (see raw envelope above)")
        return 1

    if not args.image and not any(bool(r.get("IsMatch")) for r in results):
        print('FAIL: the built-in Test hash should match (source "Test") but did not')
        return 1

    print("OK: key works and csam_screening parsed the live response correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
