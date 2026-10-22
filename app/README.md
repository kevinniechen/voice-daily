# iOS app

A Capacitor shell around the web app at https://voice-daily.exe.xyz. The app loads that site, so web updates reach the app without a new release. Native pieces: microphone (one system prompt), daily reminder notifications, and Sign in with Apple.

Bundle ID: `com.kevchn.tenminutevoice` (change it in `capacitor.config.json` and the Xcode target before the first upload if you want another).

## One-time setup (Mac)
1. Enroll in the Apple Developer Program as an individual: https://developer.apple.com/programs/enroll/
2. Install Xcode from the Mac App Store, and Node.js 20 or later.
3. ```
   git clone https://github.com/kevinniechen/voice-daily && cd voice-daily/app
   npm install
   npx cap sync ios
   npx cap open ios
   ```
4. In Xcode, select the **App** target, open **Signing & Capabilities**, choose your team, and leave "Automatically manage signing" on. The **Sign in with Apple** capability is already in `App.entitlements`; Xcode registers it with your team.

## Run on your iPhone
Plug in the phone, pick it as the run destination, and press ▶. Check that the mic works, that a lesson records and grades, and that Journey → Sign in with Apple works.

## Upload to TestFlight and the App Store
1. In App Store Connect (https://appstoreconnect.apple.com), go to My Apps → **+** → New App. Choose iOS, name "Ten Minute Voice", bundle ID `com.kevchn.tenminutevoice`, SKU `tenminutevoice`.
2. In Xcode, set **Any iOS Device (arm64)** as the destination, then **Product → Archive**. When the Organizer opens: **Distribute App → App Store Connect → Upload**.
3. In App Store Connect, the build appears under **TestFlight** after processing (about 10–30 minutes). Add yourself and friends as testers.
4. When it's ready: fill in the listing from `store/listing.md`, upload `store/screenshots/`, answer App Privacy as listed there, pick the build, and **Submit for Review**.

## Server settings to finish
- `CONTACT_EMAIL` in `~/voice-daily/.env` on the VM: the address shown on the privacy and terms pages.
- `APPLE_CLIENT_IDS` (default `com.kevchn.tenminutevoice`): add the web Services ID here if you later add Sign in with Apple on the website.
