"""Bridge: Claude/ChatGPT ask the user's own PC agent for KLAS data.

The server never logs in to KLAS and never stores personal data. It holds device public
keys, OAuth bookkeeping (grants and token hashes), and for at most two minutes an AES-GCM
ciphertext whose key only the requesting Lambda (in memory) and the PC agent know.
"""
