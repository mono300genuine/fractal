# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2024 Manifold Labs

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import time
import torch
import random
import typing
import asyncio
import string
import bittensor as bt

from fractal import protocol
from fractal.verifier.event import EventSchema
from fractal.constants import CHALLENGE_FAILURE_REWARD
from fractal.utils.uids import get_random_uids
from fractal.verifier.bonding import update_statistics, get_tier_factor
from fractal.verifier.reward import hashing_function, apply_reward_scores


def _filter_verified_responses(uids, responses):
    not_none_responses = [
        (uid, response[0])
        for (uid, (verified, response)) in zip(uids, responses)
        if verified != None
    ]

    if len(not_none_responses) == 0:
        return (), ()

    uids, responses = zip(*not_none_responses)
    return uids, responses

def verify( output, ground_truth_hash):

    output_hash = hashing_function(output)
    if not output_hash == ground_truth_hash:
        bt.logging.debug(
            f"Output hash {output_hash} does not match ground truth hash {ground_truth_hash}"
        )
        return False

    bt.logging.debug(
        f"Output hash {output_hash} matches ground truth hash {ground_truth_hash}"
    )
    return True

async def handle_challenge( self, uid: int, private_input: typing.Dict, ground_truth_hash: str, sampling_params: protocol.PromptRequestSamplingParams ) -> typing.Tuple[bool, protocol.PromptRequest]:
    """
    Handles a challenge sent to a prover and verifies the response.

    Parameters:
    - uid (int): The UID of the prover being challenged.

    Returns:
    - Tuple[bool, protocol.Challenge]: A tuple containing the verification result and the challenge.
    """

    hotkey = self.metagraph.hotkeys[uid]
    keys = await self.database.hkeys(f"hotkey:{hotkey}")
    bt.logging.trace(f"{len(keys)} stats pulled for hotkey {hotkey}")
    prompt = private_input["query"]

    if not self.config.mock:
        synapse = protocol.PromptRequest(
            query = prompt,
            sampling_params=sampling_params,
        )

        response = await self.dendrite(
            self.metagraph.axons[uid],
            synapse,
            deserialize=False,
            timeout=self.config.neuron.timeout,
        )

        output = response.completion
        
        verified = verify( output, ground_truth_hash )

        output_tup = (
            response,
            uid
        )
        return verified, output_tup
    
    else:
        synapse = protocol.PromptRequest(
            query = prompt,
            sampling_params=sampling_params,
        )

        prompt = synapse.query
        response = await self.client.generate(prompt, sampling_params.seed)
        await self.client.close_session()

        synapse.completion = response

        verified = verify( self, response, ground_truth_hash )

        output_tup = (
            synapse,
            uid
        )
        return verified, output_tup


def generate_challenge( self ):
    length = self.config.neuron.challenge_size or 100
    char_pool = string.ascii_letters + string.digits
    return ''.join(random.choice(char_pool) for i in range(length))



async def challenge_data( self ):
    
    def remove_indices_from_tensor(tensor, indices_to_remove):
        # Sort indices in descending order to avoid index out of range error
        sorted_indices = sorted(indices_to_remove, reverse=True)
        for index in sorted_indices:
            tensor = torch.cat([tensor[:index], tensor[index + 1 :]])
        return tensor

    # --- Create the event
    event = EventSchema(
        task_name="challenge",
        successful=[],
        completion_times=[],
        task_status_messages=[],
        task_status_codes=[],
        block=self.subtensor.get_current_block(),
        uids=[],
        step_length=0.0,
        best_uid=-1,
        best_hotkey="",
        rewards=[],
        set_weights=None,
        moving_averaged_scores=None,
    )

    
    prompt = generate_challenge(self)
    private_input = {'query': prompt}
    seed = random.randint(1, 2**32 - 1)

    sampling_params = protocol.PromptRequestSamplingParams(
        seed=seed,
    )

    # --- Generate the ground truth output
    ground_truth_output = await self.client.generate(prompt, seed) 
    await self.client.close_session()

    # --- get hashing function
    ground_truth_hash = hashing_function(ground_truth_output)

    # --- Get the uids to query
    start_time = time.time()
    tasks = []
    # uids = await get_tiered_uids( self, k=self.config.neuron.sample_size )
    uids = get_random_uids( self, k=self.config.neuron.sample_size )

    bt.logging.debug(f"challenge uids {uids}")
    responses = []
    for uid in uids:
        tasks.append(asyncio.create_task(handle_challenge(self, uid, private_input, ground_truth_hash, sampling_params)))
    responses = await asyncio.gather(*tasks)


    rewards: torch.FloatTensor = torch.zeros(len(responses), dtype=torch.float32).to(
        self.device
    )

    remove_reward_idxs = []
    for i, (verified, (response, uid)) in enumerate(responses):
        bt.logging.trace(
            f"Challenge iteration {i} uid {uid}"
        )

        hotkey = self.metagraph.hotkeys[uid]

        # Update the challenge statistics
        await update_statistics(
            ss58_address=hotkey,
            success=verified,
            task_type="challenge",
            database=self.database,
            current_block=self.block,
        )

        # Apply reward for this challenge
        tier_factor = await get_tier_factor(hotkey, self.database)
        rewards[i] = 1.0 * tier_factor if verified else CHALLENGE_FAILURE_REWARD

        if self.config.mock:
            event.uids.append(uid)
            event.successful.append(verified)        
            event.completion_times.append(0.0) # What is this
            event.task_status_messages.append("mock")
            event.task_status_codes.append(0)
            event.rewards.append(rewards[i].item())
        else: 
            event.uids.append(uid)
            event.successful.append(verified)
            event.completion_times.append(response.dendrite.process_time)
            event.task_status_messages.append(response.dendrite.status_message)
            event.task_status_codes.append(response.dendrite.status_code)
            event.rewards.append(rewards[i].item())

    bt.logging.debug(
        f"challenge_data() rewards: {rewards} | uids {uids} hotkeys {[self.metagraph.hotkeys[uid] for uid in uids]}"
    )

    event.step_length = time.time() - start_time

    if len(responses) == 0:
        bt.logging.debug(f"Received zero hashes from miners, returning event early.")
        return event

    # Remove UIDs without hashes (don't punish new miners that have no challenges yet)
    uids, responses = _filter_verified_responses(uids, responses)
    bt.logging.debug(
        f"challenge_data() full rewards: {rewards} | uids {uids} | uids to remove {remove_reward_idxs}"
    )
    rewards = remove_indices_from_tensor(rewards, remove_reward_idxs)
    bt.logging.debug(f"challenge_data() kept rewards: {rewards} | uids {uids}")

    bt.logging.trace("Applying challenge rewards")

    apply_reward_scores(
        self,
        uids,
        responses,
        rewards,
        timeout=self.config.neuron.timeout,
        mode=self.config.neuron.reward_mode,
    )

    # Determine the best UID based on rewards
    if event.rewards:
        best_index = max(range(len(event.rewards)), key=event.rewards.__getitem__)
        event.best_uid = event.uids[best_index]
        event.best_hotkey = self.metagraph.hotkeys[event.best_uid]
    
    return event
