MACROS = {
    "$final": "Ban decisions are final. Thank you for your attention to this matter!",
    "$ban": "Hi, after reviewing your account, we have found evidence of substantial botting/hour inflation. As a result, you have been banned from hackatime, and future Hack Club events. You can appeal this decision by sending appropriate proof to this thread.",
    "$deduct": "Hi, after reviewing your account for SoM we found evidence of significant botting/hour inflation for your project(s). As a result, you will receive a payout deduction. Please note that continuing to log fraudulent time on projects will result in a ban from hackatime, SoM, and potentially future Hack Club events.",
    "$noevidence": "We cannot share our evidence for a ban due to the reasons outlined in the hackatime ban banner.",
    "$dm": "We aren't able to share details on bans for the reasons outlined on hackatime:\n```\nWe do not disclose the patterns that were detected. Releasing this information would only benefit fraudsters. The fraud team regularly investigates claims of false bans to increase the effectiveness of our detection systems to combat fraud.\n```\nWhat I can tell you:\nYou were banned because your hackatime data matched patterns strongly indicative of fraud, and this was verified by human reviewers. Ban decisions are final and will not be lifted. If you were banned in error, the ban will automatically be lifted.",
    "$alt": "Hi, we've determined that your account is/has an alt. Alting/ban evasion are not allowed. As a result, you've been banned from hackatime, SoM, and future Hack Club events."
}

def expand_macros(text):
    if not text:
        return text

    for macro, replacement in MACROS.items():
        if macro in text:
            text = text.replace(macro, replacement)

    return text
