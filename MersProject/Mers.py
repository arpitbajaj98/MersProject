from Bio import SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from TransPlaceholder import *
import csv
from MonoAminoAndMods import *
import multiprocessing
from multiprocessing import Queue
import time
import sys
# import h5py
import json
import logging
from MGFMain import *
import atexit
import os
import psutil

TRANS = "Trans"
LINEAR = "Linear"
CIS = "Cis"

logging.basicConfig(level = logging.DEBUG, format = '%(message)s')
#logging.disable(logging.INFO)

mgfData = None

# massDict syntax: PEPTIDE: [monoisotopic mass, [referenceLocation], {charge: m/z}]
# for example: {'DQQ': [389.15466499999997, ['121', '117-118'], {2: 195.58527249999997}],
# 'QDQ': [389.15466499999997, ['118-119', '117'], {2: 195.58527249999997}]......

class Fasta:

    """
    Class that represents the input from a fasta file
    """

    def __init__(self, inputFile):
        self.inputFile = inputFile
        self.allProcessList = []
        self.pepTotal = multiprocessing.Queue()
        self.pepCompleted = multiprocessing.Queue()

    def generateOutput(self, mined, maxed, overlapFlag, transFlag, cisFlag, linearFlag, csvFlag, modList,
                       maxDistance, outputPath, chargeFlags, mgfObj, mgfFlag):

        """
        Function that literally combines everything to generate output
        """
        self.allProcessList = []
        if transFlag:

            transProcess = multiprocessing.Process(target=transOutput, args=(self.inputFile, TRANS, mined, maxed, maxDistance, overlapFlag,
                                                                             modList, outputPath[TRANS], chargeFlags,
                                                                             mgfObj, modTable, mgfFlag, self.pepCompleted, self.pepTotal, csvFlag))
            #allProcessList.append(transProcess)
            self.allProcessList.append(transProcess)
            transProcess.start()

        if cisFlag:
            cisProcess = multiprocessing.Process(target=cisAndLinearOutput, args=(self.inputFile, CIS, mined, maxed,
                                                                                  overlapFlag, csvFlag, modList,
                                                                                  maxDistance,
                                                                                  outputPath[CIS], chargeFlags, mgfObj, modTable, mgfFlag,
                                                                                  self.pepCompleted, self.pepTotal))
            self.allProcessList.append(cisProcess)
            cisProcess.start()

        if linearFlag:
            linearProcess = multiprocessing.Process(target=cisAndLinearOutput, args=(self.inputFile, LINEAR, mined,
                                                                                     maxed, overlapFlag, csvFlag,
                                                                                     modList, maxDistance,
                                                                                     outputPath[LINEAR], chargeFlags, mgfObj, modTable, mgfFlag,
                                                                                     self.pepCompleted, self.pepTotal))
            self.allProcessList.append(linearProcess)
            linearProcess.start()

        for process in self.allProcessList:
            process.join()



# def transOutput(inputPath, mined, maxed, overlapFlag, modList, outputPath, chargeFlags, linearFlag=False):
def transOutput(inputFile, spliceType, mined, maxed, maxDistance, overlapFlag,
                modList, outputPath, chargeFlags, mgfObj, modTable, mgfFlag, pepCompleted, pepTotal, csvFlag):

    finalPath = None

    # Open the csv file if the csv file is selected
    if csvFlag:
        finalPath = getFinalPath(outputPath, spliceType)
        open(finalPath, 'w')

    seqDict = addSequenceList(inputFile)

    finalPeptide, protIndexList = combinePeptides(seqDict)

    splits, splitRef = splitTransPeptide(spliceType, finalPeptide, mined, maxed, protIndexList)

    splitLen = len(splits)

    # Old code used to split up trans output differently
    # changeOver = []
    # for i in range(1, 5):
    #     val = splitLen - math.ceil(splitLen / (2 ** i))
    #     changeOver.append(val)

    # configure mutliprocessing functionality
    num_workers = multiprocessing.cpu_count()

    # Used to lock write access to file
    lockVar = multiprocessing.Lock()

    toWriteQueue = multiprocessing.Queue()

    pool = multiprocessing.Pool(processes=num_workers, initializer=processLockTrans, initargs=(lockVar, toWriteQueue, pepCompleted,
                                          splits, splitRef, mgfObj, modTable))

    writerProcess = multiprocessing.Process(target=writer, args=(toWriteQueue, outputPath))
    writerProcess.start()

    # Create a process for pairs of splits, pairing element 0 with -1, 1 with -2 and so on.
    splitsIndex = []
    procSize = 5

    # for i in range(0, math.ceil(splitLen / 2)):
    #     if splitLen % 2 == 1 and i == math.floor(splitLen / 2):
    #         splitsIndex.append(i)
    #     else:
    #         splitsIndex.append(i)
    #         splitsIndex.append(-(i + 1))
    #     print(splitsIndex)
    #     pool.apply_async(transProcess, args=(spliceType, splitsIndex, mined, maxed, maxDistance, overlapFlag, modList, outputPath, chargeFlags, mgfObj, mgfFlag))
    #     pepTotal.put(1)
    #     splitsIndex = []

    maxMem = psutil.virtual_memory()[1] / 2
    print(maxMem)

    for i in range(0, math.ceil(splitLen / 2), procSize):
        if i + procSize > math.floor(splitLen / 2):
            for j in range(i, splitLen - i):
                splitsIndex.append(j)
        else:
            for j in range(i, i + procSize):
                splitsIndex.append(j)
                splitsIndex.append(-(j + 1))
        print(splitsIndex)

        while memoryCheck(maxMem):
            time.sleep(1)
            print('stuck in memory check')

        pool.apply_async(transProcess, args=(spliceType, splitsIndex, mined, maxed, maxDistance, overlapFlag, modList, finalPath, chargeFlags, mgfObj, mgfFlag, csvFlag, protIndexList))
        pepTotal.put(1)
        splitsIndex = []

    pool.close()
    pool.join()

    toWriteQueue.put('stop')
    writerProcess.join()
    logging.info("All " + spliceType + " !joined")

    # Old code to Create processes by dividing up the splits.
    # iterCounter = math.ceil(splitLen/1000)
    # counter = 1
    # multiprocessIter = []
    # iterFlag = True
    # numOfProcesses = 0
    # while iterFlag:
    #     splitsIndex = []
    #     for i in range(0, iterCounter):
    #         splitsIndex.append(counter + i)
    #         if counter + i == splitLen - 1:
    #             iterFlag = False
    #             break
    #     counter += iterCounter
    #     #multiprocessIter.append(splitsIndex)
    #     # start process for the relevant splits
    #     pool.apply_async(transProcess, args=(spliceType,splitsIndex,mined, maxed,maxDistance,overlapFlag, modList, outputPath,chargeFlags, mgfObj, mgfFlag))
    #
    #     #pool.apply_async(tester, args=(iterCounter,))
    #     #numOfProcesses += 1
    #     pepTotal.put(1)
    #     # change number of splits in each iteration when changeover point is reached
    #     S1 = set(changeOver)
    #     S2 = set(splitsIndex)
    #     if len(S1.intersection(S2)) != 0:
    #         iterCounter = iterCounter*2

    # massDictAll = {}
    # seenPeptides = {}
    # for index in multiprocessIter:
    #     massDict = transProcess(spliceType,index,splits, splitRef, mined, maxed, maxDistance, overlapFlag,modList,outputPath, chargeFlags, mgfObj, modTable, mgfFlag)
    #     massDictAll.update(massDict)
    #     #print(massDict)
    #     for key, value in massDict.items():
    #         if key not in seenPeptides.keys():
    #             seenPeptides[key] = value
            # else:
            #     seenPeptides[key].append(value)
    #print(seenPeptides)
    #writeToCsv(seenPeptides, index, outputPath, chargeFlags)

    # allPeptides = getAllPep(massDictAll)
    # allPeptidesDict = {}
    # for peptide in allPeptides:
    #     allPeptidesDict[peptide] = [TRANS]
    # saveHandle = str(outputPath)
    # with open(saveHandle, 'w') as output_handle:
    #     SeqIO.write(createSeqObj(allPeptidesDict), output_handle, "fasta")


    #pepTotal.put(numOfProcesses)

# takes splits index from the multiprocessing pool and adds to writer the output. Splits and SplitRef are global
# variables within the pool.
def transProcess(spliceType, splitsIndex, mined, maxed, maxDistance, overlapFlag, modList, finalPath,
                 chargeFlags, mgfObj, mgfFlag, csvFlag, protIndexList):

    # Look to produce only trans spliced peptides - not linear or cis. Do so by not allowing combination of peptides
    # which originate from the same protein as opposed to solving for Cis and Linear and not including that
    # in the output
    combined, combinedRef = combineTransPeptide(splits, splitRef, mined, maxed, maxDistance, overlapFlag, splitsIndex)
    # update combineRef to include information on where the peptide originated from
    # combinedRef = findOrigProt(combinedRef, protIndexList)
    # Convert it into a dictionary that has a mass
    massDict = combMass(combined, combinedRef)
    # Apply mods to the dictionary values and update the dictionary
    massDict = applyMods(massDict, modList)
    # Add the charge information along with their masses
    chargeIonMass(massDict, chargeFlags)
    # Get the positions in range form, instead of individuals (0,1,2) -> (0-2)
    massDict = editRefMassDict(massDict)

    # #return massDict
    # allPeptides = getAllPep(massDict)
    # allPeptidesDict = {}
    # #print(allPeptides)
    # for peptide in allPeptides:
    #     allPeptidesDict[peptide] = TRANS
    # transProcess.toWriteQueue.put(allPeptidesDict)
    # transProcess.pepCompleted.put(1)

    if mgfFlag:
        allPeptides = getAllPep(massDict)
        allPeptidesDict = {}
        for peptide in allPeptides:
            allPeptidesDict[peptide] = TRANS
        transProcess.toWriteQueue.put(allPeptidesDict)

    # If there is an mgf file AND there is a charge selected
    elif mgfData is not None and True in chargeFlags:
        #fulfillPpmReq(mgfObj, massDict)
        matchedPeptides = generateMGFList(TRANS, mgfData, massDict, modList)
        transProcess.toWriteQueue.put(matchedPeptides)

    # If csv is selected, write to csv file
    if csvFlag:
        logging.info("Writing locked :(")
        lock.acquire()

        writeToCsv(massDict, TRANS, finalPath, chargeFlags)
        lock.release()
        logging.info("Writing released!")

    transProcess.pepCompleted.put(1)

def findOrigProt(combinedRef, protIndexList):
    print(combinedRef)
    print(protIndexList)

def findInitProt(index, protIndexList):
    length = protIndexList[-1][-1]
    print(length)
    aveLen = math.ceil(length/len(protIndexList))
    print(aveLen)
    protIter = math.floor(index/aveLen)
    print(protIndexList[protIter][0])
    while True:
        lower = protIndexList[protIter][0]
        upper = protIndexList[protIter][1]
        if lower <= index:
            if upper >= index:
                #print(protIndexList[protIter])
                return protIndexList[protIter]
            else:
                protIter += 1
        else:
            protIter -= 1

def splitTransPeptide(spliceType, peptide, mined, maxed, protIndexList):

    """
    Inputs: peptide string, max length of split peptide.
    Outputs: all possible splits that could be formed that are smaller in length than the maxed input
    """

    # Makes it easier to integrate with earlier iteration where linearFlag was being passed as an external flag
    # instead of spliceType
    linearFlag = spliceType == LINEAR
    length = len(peptide)

    # splits will hold all possible splits that can occur
    splits = []
    # splitRef will hold a direct reference to the characters making up each split string: for starting peptide ABC,
    # the split AC = [0,2]
    splitRef = []

    # embedded for loops build all possible splits
    for i in range(0, length):

        character = peptide[i]
        toAdd = ""

        # figure out which protein the splits starts in, and the max index the splits can reach before it becomes
        # a part of a second peptide
        initProt = findInitProt(i, protIndexList)

        # add and append first character and add and append reference number which indexes this character
        toAdd += character
        ref = list([i+1])
        temp = list(ref)  # use list because otherwise shared memory overwrites

        # linear flag to ensure min is correct for cis and trans
        if linearFlag and minSize(toAdd, mined):

            # Don't need to continue this run as first amino acid is unknown X
            if 'X' in toAdd or 'U' in toAdd:
                continue
            else:
                splits.append(toAdd)
                splitRef.append(temp)

        elif not linearFlag and 'X' not in toAdd and 'U' not in toAdd:
            splits.append(toAdd)
            splitRef.append(temp)

        # iterates through every character after current and adds it to the most recent string if max size
        # requirement is satisfied
        for j in range(i + 1, length):
            if j > initProt[1]:
                break
            toAdd += peptide[j]
            if linearFlag:
                ref.append(j+1)
                if maxSize(toAdd, maxed):
                    if minSize(toAdd, mined):

                        # All future splits will contain X if an X is found in the current run, hence break
                        if 'X' in toAdd or 'U' in toAdd:
                            break
                        splits.append(toAdd)
                        temp = list(ref)
                        splitRef.append(temp)
                else:
                    break

            else:
                if maxSize(toAdd, maxed-1):
                    # All future splits will contain X if an X is found in the current run, hence break
                    if 'X' in toAdd or 'U' in toAdd:
                        break
                    splits.append(toAdd)
                    ref.append(j+1)
                    temp = list(ref)
                    splitRef.append(temp)
                else:
                    break

    return splits, splitRef

def combineTransPeptide(splits, splitRef, mined, maxed, maxDistance, overlapFlag, splitsIndex, combineLinearSet=None):

    """
    Input: splits: list of splits, splitRef: list of the character indexes for splits, mined/maxed: min and max
    size requirements, overlapFlag: boolean value true if overlapping combinations are undesired.
    Output: all combinations of possible splits which meets criteria
    """
    # initialise combinations array to hold the possible combinations from the input splits
    combModless = []
    combModlessRef = []
    # iterate through all of the splits and build up combinations which meet min/max/overlap criteria
    for i in splitsIndex:
        # toAdd variables hold temporary combinations for insertion in final matrix if it meets criteria
        toAddForward = ""

        toAddReverse = ""


        for j in range(i + 1, len(splits)):
            # create forward combination of i and j
            toAddForward += splits[i]
            toAddForward += splits[j]
            addForwardRef = splitRef[i] + splitRef[j]
            toAddReverse += splits[j]
            toAddReverse += splits[i]
            addReverseRef = splitRef[j] + splitRef[i]

            # max, min and max distance checks combined into one function for clarity for clarity
            if combineCheck(toAddForward, mined, maxed, splitRef[i], splitRef[j], maxDistance):
                # V. messy, need a way to get better visual
                if overlapFlag:
                    if overlapComp(splitRef[i], splitRef[j]):
                        if linearCheck(toAddForward, combineLinearSet):
                            combModless.append(toAddForward)
                            combModlessRef.append(addForwardRef)
                        if linearCheck(toAddReverse, combineLinearSet):
                            combModless.append(toAddReverse)
                            combModlessRef.append(addReverseRef)

                else:
                    if linearCheck(toAddForward, combineLinearSet):
                        combModless.append(toAddForward)
                        combModlessRef.append(addForwardRef)
                    if linearCheck(toAddReverse, combineLinearSet):
                        combModless.append(toAddReverse)
                        combModlessRef.append(addReverseRef)
            elif not maxDistCheck(splitRef[i], splitRef[j], maxDistance):
                break

            toAddForward = ""
            toAddReverse = ""

    return combModless, combModlessRef

def cisAndLinearOutput(inputFile, spliceType, mined, maxed, overlapFlag, csvFlag,
                       modList, maxDistance, outputPath, chargeFlags, mgfObj, childTable, mgfFlag, pepCompleted, pepTotal):

    """
    Process that is in charge for dealing with cis and linear. Creates sub processes for every protein to compute
    their respective output
    """

    finalPath = None

    # Open the csv file if the csv file is selected
    if csvFlag:
        finalPath = getFinalPath(outputPath, spliceType)
        open(finalPath, 'w')

    num_workers = multiprocessing.cpu_count()

    # Don't need all processes for small file?
    # if len(seqDict) < num_workers:
    #     num_workers = len(seqDict)

    # Used to lock write access to file
    lockVar = multiprocessing.Lock()

    toWriteQueue = multiprocessing.Queue()

    pool = multiprocessing.Pool(processes=num_workers, initializer=processLockInit, initargs=(lockVar, toWriteQueue, pepCompleted,
                                                                                              mgfObj, childTable))

    writerProcess = multiprocessing.Process(target=writer, args=(toWriteQueue, outputPath))
    writerProcess.start()

    maxMem = psutil.virtual_memory()[1] / 2
    with open(inputFile, "rU") as handle:
        #counter = 0
        for record in SeqIO.parse(handle, 'fasta'):
            #counter += 1
            pepTotal.put(1)
            seq = str(record.seq)
            seqId = record.name

            while memoryCheck(maxMem):
                time.sleep(1)
                logging.info('Memory Limit Reached')

            seqId = seqId.split('|')[1]
            logging.info(spliceType + " process started for: " + seq[0:5])
            # Start the processes for each protein with the targe function being genMassDict
            pool.apply_async(genMassDict, args=(spliceType, seqId, seq, mined, maxed, overlapFlag,
                                                csvFlag, modList, maxDistance, finalPath, chargeFlags, mgfFlag))


    #pepTotal.put(counter)
    pool.close()
    pool.join()

    toWriteQueue.put('stop')
    writerProcess.join()
    logging.info("All " + spliceType + " !joined")

def memoryCheck(maxMem):
    process = psutil.Process(os.getpid())
    #print(process.memory_info().rss)
    if process.memory_info().rss > maxMem:
        return True
    else:
        return False

def memoryCheck2():
    memUsed = psutil.virtual_memory()[2]
    #print(memUsed)
    if memUsed > 60:
        return True
    else:
        return False

def genMassDict(spliceType, protId, peptide, mined, maxed, overlapFlag, csvFlag, modList,
                maxDistance, finalPath, chargeFlags, mgfFlag):

    """
    Compute the peptides for the given protein
    """
    start = time.time()

    # Get the initial peptides and their positions
    combined, combinedRef = outputCreate(spliceType, peptide, mined, maxed, overlapFlag, maxDistance)

    # Convert it into a dictionary that has a mass
    massDict = combMass(combined, combinedRef)
    # Apply mods to the dictionary values and update the dictionary

    massDict = applyMods(massDict, modList)

    # Add the charge information along with their masses
    chargeIonMass(massDict, chargeFlags)

    # Get the positions in range form, instead of individuals (0,1,2) -> (0-2)
    massDict = editRefMassDict(massDict)


    if mgfFlag:
        allPeptides = getAllPep(massDict)
        allPeptidesDict = {}
        for peptide in allPeptides:
            allPeptidesDict[peptide] = protId
        genMassDict.toWriteQueue.put(allPeptidesDict)
    # If there is an mgf file AND there is a charge selected
    elif mgfData is not None and True in chargeFlags:
        #fulfillPpmReq(mgfObj, massDict)
        matchedPeptides = generateMGFList(protId, mgfData, massDict, modList)
        genMassDict.toWriteQueue.put(matchedPeptides)


    # If csv is selected, write to csv file
    if csvFlag:
        logging.info("Writing locked :(")
        lock.acquire()

        writeToCsv(massDict, protId, finalPath, chargeFlags)
        lock.release()
        logging.info("Writing released!")

    end = time.time()

    logging.info(peptide[0:5] + ' took: ' + str(end-start) + ' for ' + spliceType)
    genMassDict.pepCompleted.put(1)

def getAllPep(massDict):

    allPeptides = set()
    for key, value in massDict.items():
        if not key.isalpha():
            alphaKey = modToPeptide(key)
        else:
            alphaKey = key
        allPeptides.add(alphaKey)
    return allPeptides

def writer(queue, outputPath):
    seenPeptides = {}
    backwardsSeenPeptides = {}
    saveHandle = str(outputPath)
    with open(saveHandle, "w") as output_handle:
        while True:
            matchedPeptides = queue.get()
            if matchedPeptides == 'stop':
                logging.info("ALL LINEAR COMPUTED, STOP MESSAGE SENT")
                break

            # if type(matchedPeptides) == set:
            #     for peptide in matchedPeptides:

            for key, value in matchedPeptides.items():
                if key not in seenPeptides.keys():

                    seenPeptides[key] = [value]
                else:
                    if value not in seenPeptides[key]:
                        seenPeptides[key].append(value)
                    #seenPeptides[key].append(value)

                # Come back to make this less ugly and more efficient
                if value not in backwardsSeenPeptides.keys():
                    backwardsSeenPeptides[value] = [key]
                else:
                    backwardsSeenPeptides[value].append(key)

        logging.info("Writing to fasta")
        writeProtToPep(backwardsSeenPeptides, 'ProtToPep', outputPath)
        writeProtToPep(seenPeptides, 'PepToProt', outputPath)
        SeqIO.write(createSeqObj(seenPeptides), output_handle, "fasta")

def writeProtToPep(seenPeptides, groupedBy, outputPath):
    with open(outputPath+ groupedBy + '.csv', 'a', newline='') as csv_file:

        writer = csv.writer(csv_file, delimiter=',')
        if groupedBy is 'ProtToPep':
            header = 'Protein'
        else:
            header = 'Peptide'
        writer.writerow([header])
        for key, value in seenPeptides.items():
            infoRow = [key]
            writer.writerow(infoRow)
            for peptide in value:
                writer.writerow([peptide])
            writer.writerow([])


def fulfillPpmReq(mgfObj, massDict):
    """
    Assumption there are charges. Get the peptides that match, and writes them to the output fasta file
    """

    matchedPeptides = generateMGFList(mgfObj, massDict)

    lock.acquire()
    logging.info("Writing to fasta")
    with open("OutputMaster.fasta", "a") as output_handle:
        SeqIO.write(createSeqObj(matchedPeptides), output_handle, "fasta")

    lock.release()
    logging.info("Writing complete")


def createSeqObj(matchedPeptides):
    """
    Given the set of matchedPeptides, converts all of them into SeqRecord objects and passes back a generator
    """
    count = 1
    seqRecords = []
    for sequence, value in matchedPeptides.items():

        finalId = "ipd|pep"+str(count)+';'

        for protein in value:
            finalId+=protein+';'

        yield SeqRecord(Seq(sequence), id=finalId, description="")

        count += 1

    return seqRecords


# set default maxDistance to be absurdly high incase of trans
def outputCreate(spliceType, peptide, mined, maxed, overlapFlag, maxDistance=10000000):

    # Splits eg: ['A', 'AB', 'AD', 'B', 'BD']
    # SplitRef eg: [[0], [0,1], [0,2], [1], [1,2]]
    # Produces splits and splitRef arrays which are passed through combined
    splits, splitRef = splitDictPeptide(spliceType, peptide, mined, maxed)
    combined, combinedRef = None, None

    if spliceType == CIS:

        # get the linear set to ensure no linear peptides are added to cis set. ( Redoing is a little redundant,
        # need to find something better )
        combineLinear, combineLinearRef = splitDictPeptide(LINEAR, peptide, mined, maxed)

        combineLinearSet = set(combineLinear)

        # combined eg: ['ABC', 'BCA', 'ACD', 'DCA']
        # combinedRef eg: [[0,1,2], [1,0,2], [0,2,3], [3,2,0]]
        # pass splits through combined overlap peptide and then delete all duplicates

        combined, combinedRef = combineOverlapPeptide(splits, splitRef, mined, maxed, overlapFlag, maxDistance,
                                                      combineLinearSet)
    elif spliceType == TRANS:
        combined, combinedRef = combineOverlapPeptide(splits, splitRef, mined, maxed, overlapFlag, maxDistance)
    elif spliceType == LINEAR:

        # Explicit change for high visibility regarding what's happening
        combined, combinedRef = splits, splitRef

    combined, combinedRef = removeDupsQuick(combined, combinedRef)

    return combined, combinedRef

def applyMods(combineModlessDict, modList):

    """
    Calls the genericMod function and accesses the modification table to
    append modified combinations to the end of the combination dictionary
    """

    modNo = 0
    for mod in modList:
        # Keep track of which modification is taking place
        modNo += 1

        # Don't need to worry about it if no modification!
        if mod != 'None':
            # Get the list of modifications taking place
            aminoList = finalModTable[mod]
           
            # Go through each character in the modification one by one
            for i in range(0, len(aminoList) - 1):

                char = aminoList[i]
                massChange = aminoList[-1]
                # get the dictionary of mods and their mass
                modDict = genericMod(combineModlessDict, char, massChange, str(modNo))
                # Add it to the current list!
                combineModlessDict.update(modDict)
    return combineModlessDict


def genericMod(combineModlessDict, character, massChange, modNo):

    """
    From the modless dictionary of possible combinations, this function returns a
    dictionary containing all the modifications that arise from changing character. The
    key of the output is simply the modified peptide, and the value is the mass which
    results as set by massChange
    """
    # A, B, C  convert to ai, bi, ci where i is the modNo
    modDict = {}

    # Go through each combination and mod it if necessary
    for string in combineModlessDict.keys():

        currentMass = combineModlessDict[string][0]
        currentRef = combineModlessDict[string][1]

        # Only need to mod it if it exists (ie : A in ABC)
        if character in string:

            numOccur = string.count(character)
            # Generate all permutations with the mods
            for j in range(0, numOccur):
                temp = string
                for i in range(0, numOccur - j):
                    newMass = currentMass + (i + 1) * massChange
                    temp = nth_replace(temp, character, character.lower() + modNo, j + 1)

                    modDict[temp] = [newMass, currentRef]

    return modDict


def splitDictPeptide(spliceType, peptide, mined, maxed):

    """
    Inputs: peptide string, max length of split peptide.
    Outputs: all possible splits that could be formed that are smaller in length than the maxed input
    """

    # Makes it easier to integrate with earlier iteration where linearFlag was being passed as an external flag
    # instead of spliceType
    linearFlag = spliceType == LINEAR
    length = len(peptide)

    # splits will hold all possible splits that can occur
    splits = []
    # splitRef will hold a direct reference to the characters making up each split string: for starting peptide ABC,
    # the split AC = [0,2]
    splitRef = []

    # embedded for loops build all possible splits
    for i in range(0, length):

        character = peptide[i]
        toAdd = ""
        # add and append first character and add and append reference number which indexes this character

        toAdd += character
        ref = list([i+1])
        temp = list(ref)  # use list because otherwise shared memory overwrites

        # linear flag to ensure min is correct for cis and trans
        if linearFlag and minSize(toAdd, mined):

            # Don't need to continue this run as first amino acid is unknown X
            if 'X' in toAdd or 'U' in toAdd:
                continue
            else:
                splits.append(toAdd)
                splitRef.append(temp)

        elif not linearFlag and 'X' not in toAdd and 'U' not in toAdd:
            splits.append(toAdd)
            splitRef.append(temp)

        # iterates through every character after current and adds it to the most recent string if max size
        # requirement is satisfied
        for j in range(i + 1, length):
            toAdd += peptide[j]
            if linearFlag:
                ref.append(j+1)
                if maxSize(toAdd, maxed):
                    if minSize(toAdd, mined):

                        # All future splits will contain X if an X is found in the current run, hence break
                        if 'X' in toAdd or 'U' in toAdd:
                            break
                        splits.append(toAdd)
                        temp = list(ref)
                        splitRef.append(temp)
                else:
                    break

            else:
                if maxSize(toAdd, maxed-1):
                    # All future splits will contain X if an X is found in the current run, hence break
                    if 'X' in toAdd or 'U' in toAdd:
                        break
                    splits.append(toAdd)
                    ref.append(j+1)
                    temp = list(ref)
                    splitRef.append(temp)
                else:
                    break

    return splits, splitRef


def combineOverlapPeptide(splits, splitRef, mined, maxed, overlapFlag, maxDistance, combineLinearSet=None):

    """
    Input: splits: list of splits, splitRef: list of the character indexes for splits, mined/maxed: min and max
    size requirements, overlapFlag: boolean value true if overlapping combinations are undesired.
    Output: all combinations of possible splits which meets criteria
    """
    # initialise combinations array to hold the possible combinations from the input splits
    combModless = []
    combModlessRef = []
    # iterate through all of the splits and build up combinations which meet min/max/overlap criteria
    for i in range(0, len(splits)):

        # toAdd variables hold temporary combinations for insertion in final matrix if it meets criteria
        toAddForward = ""

        toAddReverse = ""

        for j in range(i + 1, len(splits)):
            # create forward combination of i and j
            toAddForward += splits[i]
            toAddForward += splits[j]
            addForwardRef = splitRef[i] + splitRef[j]
            toAddReverse += splits[j]
            toAddReverse += splits[i]
            addReverseRef = splitRef[j] + splitRef[i]

            # max, min and max distance checks combined into one function for clarity for clarity
            if combineCheck(toAddForward, mined, maxed, splitRef[i], splitRef[j], maxDistance):
                # V. messy, need a way to get better visual
                if overlapFlag:
                    if overlapComp(splitRef[i], splitRef[j]):
                        if linearCheck(toAddForward, combineLinearSet):
                            combModless.append(toAddForward)
                            combModlessRef.append(addForwardRef)
                        if linearCheck(toAddReverse, combineLinearSet):
                            combModless.append(toAddReverse)
                            combModlessRef.append(addReverseRef)

                else:

                    if linearCheck(toAddForward, combineLinearSet):
                        combModless.append(toAddForward)
                        combModlessRef.append(addForwardRef)
                    if linearCheck(toAddReverse, combineLinearSet):
                        combModless.append(toAddReverse)
                        combModlessRef.append(addReverseRef)
            elif not maxDistCheck(splitRef[i], splitRef[j], maxDistance):
                break

            toAddForward = ""
            toAddReverse = ""

    return combModless, combModlessRef


def chargeIonMass(massDict, chargeFlags):

    """
    chargeFlags: [True, False, True, False, True]
    """

    for key, value in massDict.items():
        chargeAssoc = {}
        for z in range(0, len(chargeFlags)):

            if chargeFlags[z]:
                chargeMass = massCharge(value[0], z+1)  # +1 for actual value
                chargeAssoc[z+1] = chargeMass
        value.append(chargeAssoc)


def massCharge(predictedMass, z):
    chargeMass = (predictedMass + (z * 1.00794))/z
    return chargeMass


def writeToCsv(massDict, header, finalPath, chargeFlags):

    chargeHeaders = getChargeIndex(chargeFlags)

    with open(finalPath, 'a', newline='') as csv_file:

        writer = csv.writer(csv_file, delimiter=',')
        writer.writerow([header, ' ', ' '])
        headerRow = ['Peptide', 'Mass', 'Positions']

        for chargeIndex in chargeHeaders:

            headerRow.append('+' + str(chargeIndex+1))

        writer.writerow(headerRow)
        for key, value in massDict.items():
            infoRow = [key, value[0], value[1]]
            for chargeIndex in chargeHeaders:
                chargeMass = value[2][chargeIndex+1]
                infoRow.append(str(chargeMass))
            writer.writerow(infoRow)
            # BREAK FOR TESTING!!!!!
            # break;


def getChargeIndex(chargeFlags):
    chargeHeaders = [i for i, e in enumerate(chargeFlags) if e]
    return chargeHeaders


def maxDistCheck(ref1, ref2, maxDistance):
    if maxDistance == 'None':
        return True
    # max distance defined as the number of peptides between to peptide strands
    valid = ref2[0] - ref1[-1] - 1
    if valid > maxDistance:
        return False
    return True


def maxSize(split, maxed):

    """
    ensures length of split is smaller than or equal to max
    """

    if len(split) > maxed:
        return False
    return True


def minSize(split, mined):

    """
    ensures length of split is greater than min
    """

    if len(split) < mined:
        return False
    return True


def combineCheck(split, mined, maxed, ref1, ref2, maxDistance='None'):
    booleanCheck = maxSize(split, maxed) and minSize(split, mined) and maxDistCheck(ref1, ref2, maxDistance)
    return booleanCheck


def linearCheck(toAdd, combinedLinearSet):
    # Look to do this better, not comparing to combineLinearSet, instead checking that the splitsRefs aren't linearly
    # ordered: [1, 2, 3] and [4, 5, 6] are obviously linearly ordered.
    if combinedLinearSet is None:
        return True
    if toAdd in combinedLinearSet:
        return False
    return True


def overlapComp(ref1, ref2):

    """
    checks if there is an intersection between two strings. Likely input it the splitRef data.
    Outputs True if no intersection
    overlapComp needs to delete paired reference number if being applied to the splits output
    """
    S1 = set(ref1)
    S2 = set(ref2)
    if len(S1.intersection(S2)) == 0:
        return True
    return False


def addSequenceList(input_file):

    """
    input_file is the file path to the fasta file.
    The function reads the fasta file into a dictionary with the format {proteinRef: protein}
    """

    fasta_sequences = SeqIO.parse(open(input_file), 'fasta')
    sequenceDictionary = {}
    for fasta in fasta_sequences:
        name, sequence = fasta.id, str(fasta.seq)


        name = name.split('|')[1]

        sequenceDictionary[name] = sequence
    return sequenceDictionary


def combinePeptides(seqDict):

    """
    combines an array of strings into one string. Used for ultimately segments from multiple peptides
    """

    dictlist = []
    protIndexList = []
    ind = 0
    for key, value in seqDict.items():
        dictlist.append(value)
        protIndexList.append([ind,ind + len(value) - 1])
        ind += len(value)

    print(protIndexList)

    finalPeptide = ''.join(dictlist)
    return finalPeptide, protIndexList


def removeDupsQuick(seq, seqRef):

    seen = set()
    seen_add = seen.add
    initial = []
    initialRef = []
    # initial = [x for x in seq if not (x in seen or seen_add(x))]
    for i in range(0, len(seq)):
        if not (seq[i] in seen or seen_add(seq[i])):
            initial.append(seq[i])
            initialRef.append(seqRef[i])

    return initial, initialRef


def combMass(combine, combineRef):
    massDict = {}
    for i in range(0, len(combine)):
        totalMass = 0
        for j in range(0, len(combine[i])):
            totalMass += monoAminoMass[combine[i][j]]
        totalMass += H20_MASS
        massRefPair = [totalMass, combineRef[i]]
        massDict[combine[i]] = massRefPair
    return massDict


def changeRefToDash(ref):
    newRef = []
    for i in range(0,len(ref)):
        refVal = ref[i]
        # check if last element reached and thus input is linear
        if i == len(ref)-1:
            newLinRef = str(ref[0]) + ' - ' + str(ref[-1])
            newRef = [newLinRef]
            return newRef
        # otherwise, check if the next element is still sequential, and if so continue for loop
        if refVal + 1 == ref[i+1]:
            continue
        else:
            if i == 0:
                newStrRef1 = str(ref[0])
            else:
                newStrRef1 = str(ref[0]) + "-" + str(ref[i])
            if i + 1 == len(ref) - 1:
                newStrRef2 = str(ref[-1])
            else:
                newStrRef2 = str(ref[i+1]) + "-" + str(ref[-1])

            newRef = [newStrRef1, newStrRef2]
            return newRef


def editRefMassDict(massDict):
    for key, value in massDict.items():
        refNew = changeRefToDash(value[1])
        value[1] = refNew
    return massDict


def getFinalPath(outputPath, spliceType):
    outputPathSmall = outputPath[0:-6]
    newPath = str(outputPathSmall) + '-' + spliceType + '.csv'
    return newPath

def nth_replace(string, old, new, n=1, option='only nth'):

    # https://stackoverflow.com/questions/35091557/replace-nth-occurrence-of-substring-in-string
    """
    This function replaces occurrences of string 'old' with string 'new'.
    There are three types of replacement of string 'old':
    1) 'only nth' replaces only nth occurrence (default).
    2) 'all left' replaces nth occurrence and all occurrences to the left.
    3) 'all right' replaces nth occurrence and all occurrences to the right.
    """

    if option == 'only nth':
        left_join = old
        right_join = old
    elif option == 'all left':
        left_join = new
        right_join = old
    elif option == 'all right':
        left_join = old
        right_join = new
    else:
        print("Invalid option. Please choose from: 'only nth' (default), 'all left' or 'all right'")
        return None
    groups = string.split(old)
    nth_split = [left_join.join(groups[:n]), right_join.join(groups[n:])]
    return new.join(nth_split)


def processLockInit(lockVar, toWriteQueue, pepCompleted, mgfObj, childTable):

    """
    Designed to set up a global lock for a child processes (child per protein)
    """

    global lock
    lock = lockVar
    global mgfData
    mgfData = mgfObj
    global finalModTable
    finalModTable = childTable
    genMassDict.toWriteQueue = toWriteQueue
    genMassDict.pepCompleted = pepCompleted

def processLockTrans(lockVar, toWriteQueue, pepCompleted, allSplits, allSplitRef, mgfObj, childTable):

    """
    Designed to set up a global lock for a child processes (child per protein)
    """
    global lock
    lock = lockVar
    global mgfData
    mgfData = mgfObj
    global finalModTable
    finalModTable = childTable
    global splits
    splits = allSplits
    global splitRef
    splitRef = allSplitRef
    transProcess.toWriteQueue = toWriteQueue
    transProcess.pepCompleted = pepCompleted


