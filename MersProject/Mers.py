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
import tempfile
from queue import Queue

TRANS = "Trans"
LINEAR = "Linear"
CIS = "Cis"

MEMORY_THRESHOLD = 80

logging.basicConfig(level=logging.DEBUG, format='%(message)s')
# logging.disable(logging.INFO)

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

    def generateOutput(self, mined, maxed, overlapFlag, transFlag, cisFlag, linearFlag, csvFlag, pepToProtFlag,
                       protToPepFlag, modList, maxDistance, outputPath, chargeFlags, mgfObj, mgfFlag):

        """
        Function that literally combines everything to generate output
        """

        self.allProcessList = []

        if transFlag:

            transProc = multiprocessing.Process(target=transOutput, args=(self.inputFile, TRANS, mined, maxed,
                                                                          maxDistance, overlapFlag, modList,
                                                                          outputPath[TRANS], chargeFlags, mgfObj,
                                                                          modTable, mgfFlag, self.pepCompleted,
                                                                          self.pepTotal, csvFlag, pepToProtFlag,
                                                                          protToPepFlag))
            self.allProcessList.append(transProc)
            transProc.start()

        if cisFlag:
            cisProcess = multiprocessing.Process(target=cisAndLinearOutput, args=(self.inputFile, CIS, mined, maxed,
                                                                                  overlapFlag, csvFlag, pepToProtFlag,
                                                                                  protToPepFlag, modList,
                                                                                  maxDistance, outputPath[CIS],
                                                                                  chargeFlags, mgfObj, modTable,
                                                                                  mgfFlag, self.pepCompleted,
                                                                                  self.pepTotal))
            self.allProcessList.append(cisProcess)
            cisProcess.start()

        if linearFlag:
            linearProcess = multiprocessing.Process(target=cisAndLinearOutput, args=(self.inputFile, LINEAR, mined,
                                                                                     maxed, overlapFlag, csvFlag,
                                                                                     pepToProtFlag, protToPepFlag,
                                                                                     modList, maxDistance,
                                                                                     outputPath[LINEAR], chargeFlags,
                                                                                     mgfObj, modTable, mgfFlag,
                                                                                     self.pepCompleted, self.pepTotal))
            self.allProcessList.append(linearProcess)
            linearProcess.start()

        for process in self.allProcessList:
            process.join()


def transOutput(inputFile, spliceType, mined, maxed, maxDistance, overlapFlag,
                modList, outputPath, chargeFlags, mgfObj, modTable, mgfFlag, pepCompleted, pepTotal, csvFlag,
                pepToProtFlag, protToPepFlag):

    finalPath = None

    # Open the csv file if the csv file is selected
    if csvFlag:
        finalPath = getFinalPath(outputPath, spliceType)
        open(finalPath, 'w')

    seqDict = addSequenceList(inputFile)

    if len(seqDict) <= 1:
        logging.info('Only 1 protein, therefore trans not relevant')
        return

    finalPeptide, protIndexList, protList = combinePeptides(seqDict)

    # temporary creation of cis peptides
    # combineCisSet = set()
    # for value in seqDict.values():
    #     splitsCis, splitRefCis = splitDictPeptide(CIS, value, mined, maxed)
    #
    #     combinedCis, combinedRefCis = combineOverlapPeptide(splitsCis, splitRefCis, mined, maxed, overlapFlag,
    #                                                         maxDistance)
    #
    #     combineCisSet.update(combinedCis)

    splits, splitRef = splitTransPeptide(spliceType, finalPeptide, mined, maxed, protIndexList)

    splitLen = len(splits)

    # configure mutliprocessing functionality
    num_workers = multiprocessing.cpu_count()

    # Used to lock write access to file
    lockVar = multiprocessing.Lock()

    toWriteQueue = multiprocessing.Queue()
    linCisQueue = multiprocessing.Queue()

    pool = multiprocessing.Pool(processes=num_workers, initializer=processLockTrans, initargs=(lockVar, toWriteQueue,
                                                                                               pepCompleted, splits,
                                                                                               splitRef, mgfObj,
                                                                                               modTable, linCisQueue))

    writerProcess = multiprocessing.Process(target=writer, args=(toWriteQueue, outputPath, linCisQueue, pepToProtFlag,
                                                                 protToPepFlag, True))
    writerProcess.start()

    # Create a process for pairs of splits, pairing element 0 with -1, 1 with -2 and so on.
    splitsIndex = []
    procSize = 5

    maxMem = psutil.virtual_memory()[1] / 2

    for i in range(0, math.ceil(splitLen / 2), procSize):
        if i + procSize > math.floor(splitLen / 2):
            for j in range(i, splitLen - i):
                splitsIndex.append(j)
        else:
            for j in range(i, i + procSize):
                splitsIndex.append(j)
                splitsIndex.append(splitLen - 1 - j)

        while memoryCheck(maxMem):
            time.sleep(1)
            print('stuck in memory check')

        pool.apply_async(transProcess, args=(spliceType, splitsIndex, mined, maxed, maxDistance, False, modList,
                                             finalPath, chargeFlags, mgfObj, mgfFlag, csvFlag, protIndexList, protList))
        pepTotal.put(1)
        splitsIndex = []

    pool.close()
    pool.join()

    toWriteQueue.put('stop')
    writerProcess.join()
    logging.info("All " + spliceType + " !joined")

# takes splits index from the multiprocessing pool and adds to writer the output. Splits and SplitRef are global
# variables within the pool.
def transProcess(spliceType, splitsIndex, mined, maxed, maxDistance, overlapFlag, modList, finalPath,
                 chargeFlags, mgfObj, mgfFlag, csvFlag, protIndexList, protList):

    # Look to produce only trans spliced peptides - not linear or cis. Do so by not allowing combination of peptides
    # which originate from the same protein as opposed to solving for Cis and Linear and not including that
    # in the output
    combined, combinedRef, linCisSet = combineTransPeptide(splits, splitRef, mined, maxed, maxDistance,
                                                           overlapFlag, splitsIndex, protIndexList)

    # Put linCisSet to linCisQueue:
    transProcess.linCisQueue.put(linCisSet)

    # update combineRef to include information on where the peptide originated from
    origProtTups = findOrigProt(combinedRef, protIndexList, protList)

    # Convert it into a dictionary that has a mass
    massDict = combMass(combined, combinedRef, origProtTups)

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
            # create the string, with peptides sorted so all permutations are matched as similar. There may be multiple
            # peptide locations in the list of tuples, hence the for loop. Tuples are listed in order, with consecutive
            # tuples relating to a pair of splice locations.
            string = ""
            for i in range(0, len(massDict[peptide][3]), 2):
                origProt = sorted(massDict[peptide][3][i:i+2])
                string += origProt[0][0] + origProt[0][1] + '/' + origProt[1][0] + origProt[1][1] + ';'
            string = string[0:-1]
            allPeptidesDict[peptide] = string
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


# Only works if we presume Cis proteins aren't being created in the trans process.
def findOrigProt(combinedRef, protIndexList, protList):
    proteinTups = []
    for i in range(0, len(combinedRef)):
        protRef1 = ""
        protRef2 = ""
        ref = combinedRef[i]
        protIndex1, protIter1 = findInitProt(ref[0] - 1, protIndexList)
        #print(protIndex1)
        prot1 = protList[protIter1]

        # special check if peptide is ovelap spliced
        if len(set(ref)) != len(ref):
            proteinTups.append([(prot1, ""),('Overlap',"")])

        for j in range(1,len(ref)):
            #print(j)
            if ref[j] - 1 > protIndex1[1] or ref[j] - 1 < protIndex1[0]:
                #check to see if the first split is at least 6 amino acids in length.
                # if so append the location of the split within the peptide to prot1
                if j > 5:
                    protRef1 += ('(' + str(ref[0] - protIndex1[0]))
                    protRef1 += ('-' + str(ref[j-1] - protIndex1[0]) + ')')

                protIndex2, protIter2 = findInitProt(ref[j] - 1, protIndexList)
                prot2 = protList[protIter2]
                # same as above, check if second split is at least 6 amino acids long
                if len(ref) - j > 5:
                    protRef2 += ('(' + str(ref[j] - protIndex2[0]))
                    protRef2 += ('-' + str(ref[-1] - protIndex2[0]) + ')')

                proteinTups.append([(prot1,protRef1),(prot2,protRef2)])
                # combinedRef[i].insert(j, prot2)
                # combinedRef[i].insert(0,prot1)
                break
    return proteinTups


def findInitProt(index, protIndexList):
    length = protIndexList[-1][-1]
    #print(length)
    # plus 1 needed for when Protein length is perfectly divisible by protein index length
    aveLen = math.ceil(length/len(protIndexList)) + 1
    #print(aveLen)
    protIter = math.floor(index/aveLen)
    #print(protIndexList[protIter][0])
    if protIter == len(protIndexList):
        protIter -= 1
        #print(protIter)
    while True:
        lower = protIndexList[protIter][0]
        upper = protIndexList[protIter][1]
        if lower <= index:
            if upper >= index:
                #print(protIndexList[protIter])
                return protIndexList[protIter], protIter
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
        initProt, protInd = findInitProt(i, protIndexList)

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


def combineTransPeptide(splits, splitRef, mined, maxed, maxDistance, overlapFlag, splitsIndex, protIndexList):

    """
    Input: splits: list of splits, splitRef: list of the character indexes for splits, mined/maxed: min and max
    size requirements, overlapFlag: boolean value true if overlapping combinations are undesired.
    Output: all combinations of possible splits which meets criteria
    """
    # initialise linCisVariable holder.
    linCisSet = set()
    # initialise combinations array to hold the possible combinations from the input splits
    combModless = []
    combModlessRef = []

    # iterate through all of the splits and build up combinations which meet min/max/overlap criteria
    for i in splitsIndex:
        # toAdd variables hold temporary combinations for insertion in final matrix if it meets criteria
        toAddForward = ""

        toAddReverse = ""

        for j in range(i, len(splits)):
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
                        # check if linear and add to linearSet if so
                        linCisSet = addLinPeptides(toAddForward, addForwardRef, linCisSet, protIndexList)
                        linCisSet = addLinPeptides(toAddReverse, addReverseRef, linCisSet, protIndexList)
                        combModless.append(toAddForward)
                        combModlessRef.append(addForwardRef)
                        combModless.append(toAddReverse)
                        combModlessRef.append(addReverseRef)

                else:
                    # check if linear and add to linearSet if so
                    linCisSet = addLinPeptides(toAddForward, addForwardRef, linCisSet, protIndexList)
                    linCisSet = addLinPeptides(toAddReverse, addReverseRef, linCisSet, protIndexList)
                    combModless.append(toAddForward)
                    combModlessRef.append(addForwardRef)
                    combModless.append(toAddReverse)
                    combModlessRef.append(addReverseRef)

            elif not maxDistCheck(splitRef[i], splitRef[j], maxDistance):
                break

            toAddForward = ""
            toAddReverse = ""

    return combModless, combModlessRef, linCisSet

def cisAndLinearOutput(inputFile, spliceType, mined, maxed, overlapFlag, csvFlag, pepToProtFlag, protToPepFlag,
                       modList, maxDistance, outputPath, chargeFlags, mgfObj, childTable, mgfFlag, pepCompleted,
                       pepTotal):

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
    linSetQueue = multiprocessing.Queue()
    pool = multiprocessing.Pool(processes=num_workers, initializer=processLockInit,
                                initargs=(lockVar, toWriteQueue, pepCompleted,
                                          mgfObj, childTable, linSetQueue))
    writerProcess = multiprocessing.Process(target=writer, args=(toWriteQueue, outputPath, linSetQueue, pepToProtFlag,
                                                                 protToPepFlag))
    writerProcess.start()

    maxMem = psutil.virtual_memory()[1] / 2



    with open(inputFile, "rU") as handle:
        for record in SeqIO.parse(handle, 'fasta'):

            pepTotal.put(1)
            seq = str(record.seq)
            seqId = record.name

            # while memoryCheck(maxMem):
            #     time.sleep(1)
            #     logging.info('Memory Limit Reached')

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

    # Get the initial peptides and their positions, and the set of linear peptides produced for this protein
    combined, combinedRef, linSet = outputCreate(spliceType, peptide, mined, maxed, overlapFlag, maxDistance)

    # add this set of linear proteins to the linProt queue
    genMassDict.linSetQueue.put(linSet)

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

def memory_usage_psutil():
    # return the memory usage in percentage like top
    process = psutil.Process(os.getpid())
    mem = process.memory_percent()
    return mem

def writer(queue, outputPath, linCisQueue, pepToProtFlag, protToPepFlag, transFlag = False):

    seenPeptides = {}
    backwardsSeenPeptides = {}
    linCisSet = set()
    saveHandle = str(outputPath)

    outputTempFiles = Queue()

    with open(saveHandle, "w") as output_handle:
        while True:
            # get from cisLinQueue and from matchedPeptide Queue
            matchedPeptides = queue.get()
            if not linCisQueue.empty():
                linCisSet = linCisSet | linCisQueue.get()
            # if stop is sent to the matchedPeptide Queue, everything has been output,
            # so we exit the while loop.
            if matchedPeptides == 'stop':
                logging.info("Everything computed, stop message has been sent")
                break

            # each queue.get() returns the matchedPeptides from an individual process.
            # Add  the matchedPeptides from the given process to seenPeptides.
            for key, value in matchedPeptides.items():
                origins = value.split(';')
                if key not in seenPeptides.keys():
                    seenPeptides[key] = origins
                else:
                    if value not in seenPeptides[key]:
                        seenPeptides[key] += origins

            # If current memory is above threshold write to a tempfile and add that to the outputTempFiles queue
            if memory_usage_psutil() > MEMORY_THRESHOLD:
                # remove linear/cis peptides from seenPeptides:
                commonPeptides = linCisSet.intersection(seenPeptides.keys())
                for peptide in commonPeptides:
                    del seenPeptides[peptide]
                tempName = writeTempFasta(seenPeptides)

                outputTempFiles.put(tempName)
                seenPeptides = {}
                backwardsSeenPeptides = {}

        # Was not over memory threshold but last few items left are also written to tempFile
        commonPeptides = linCisSet.intersection(seenPeptides.keys())
        for peptide in commonPeptides:
            del seenPeptides[peptide]

        # if no tempFiles have been generated so far, meaning the memory limit was never exceded,
        # seenPeptides already contains all the peptides generated.
        if outputTempFiles.empty():
            finalSeenPeptides = seenPeptides
        # if we have created at least one tempFile, write the remaining sequences to tempFile
        # then combine all temp files.
        else:
            tempName = writeTempFasta(seenPeptides)
            outputTempFiles.put(tempName)
            finalSeenPeptides = combineAllTempFasta(linCisSet, outputTempFiles)

        # generate backwardSeenPeptides if protToPep is selected
        if protToPepFlag:
            # convert seen peptides to backwardsSeenPeptides
            for key, value in finalSeenPeptides.items():
                # check if we are printing trans entries so that we can configure trans data
                # before it goes into backwardsSeenPeptides
                if transFlag:
                    origins = editTransOrigins(value)
                else:
                    origins = value

                # Come back to make this less ugly and more efficient
                for entry in origins:
                    if entry not in backwardsSeenPeptides.keys():
                        backwardsSeenPeptides[entry] = [key]
                    else:
                        backwardsSeenPeptides[entry].append(key)
            writeProtToPep(backwardsSeenPeptides, 'ProtToPep', outputPath)

        if pepToProtFlag:

            writeProtToPep(finalSeenPeptides, 'PepToProt', outputPath)

        logging.info("Writing to fasta")
        SeqIO.write(createSeqObj(finalSeenPeptides), output_handle, "fasta")



def combineAllTempFasta(linCisSet, outputTempFiles):

    seenPeptides = {}
    while not outputTempFiles.empty():

        # Get the two files at the top of the tempFiles queue for combination.
        # Note that there will never be one temp file in the queue when the
        # while loop is being checked, so you will always be able to get two
        # temp files from the queue if it passes the not empty check.
        fileOne = outputTempFiles.get()
        fileTwo = outputTempFiles.get()

        # if this reduces the queue to empty, break the loop. We do this to avoid merging
        # the last two temp files, adding the result to the queue and then passing a queue with
        # only one temp file in it into the while loop.
        if outputTempFiles.empty():
            break

        # when there are still more temp files in the queue, extract seenPeptides from the
        # current two temp files, write them to a new tempFile and add it to the temp file Queue.
        seenPeptides = combineTempFile(linCisSet, fileOne, fileTwo)
        tempName = writeTempFasta(seenPeptides)
        outputTempFiles.put(tempName)

    # once the while loop breaks, return the finalSeenPetides from the remaining two tempFiles.
    finalSeenPeptides = combineTempFile(linCisSet, fileOne, fileTwo)

    # Return the last combination of two files remaining
    return finalSeenPeptides

def combineTempFile(linCisSet, fileOne, fileTwo):
    logging.info("Combining two files !")
    seenPeptides = {}
    with open(fileOne, 'rU') as handle:
        for record in SeqIO.parse(handle, 'fasta'):

            peptide = str(record.seq)
            protein = str(record.name)
            if peptide not in seenPeptides.keys():
                seenPeptides[peptide] = [protein]
            else:
                seenPeptides[peptide].append(protein)
    with open(fileTwo, 'rU') as handle:
        for record in SeqIO.parse(handle, 'fasta'):

            peptide = str(record.seq)
            protein = str(record.name)
            if peptide not in seenPeptides.keys():
                seenPeptides[peptide] = [protein]
            else:
                seenPeptides[peptide].append(protein)
    # Delete temp files as they are used up
    os.remove(fileOne)
    os.remove(fileTwo)
    commonPeptides = linCisSet.intersection(seenPeptides.keys())
    for peptide in commonPeptides:
        del seenPeptides[peptide]

    return seenPeptides


def writeTempFasta(seenPeptides):
    logging.info("Writing to temp")
    temp = tempfile.NamedTemporaryFile(mode='w+t', suffix=".fasta", delete=False)
    for key, value in seenPeptides.items():
        temp.writelines(">")
        for protein in value:
            temp.writelines(str(protein))
        temp.writelines("\n")
        temp.writelines(str(key))
        temp.writelines("\n")
    return temp.name

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

def editTransOrigins(origins):
    newOrigins = []
    for entry in origins:
        prots = entry.split('/')
        for prot in prots:
            if prot[-1] == ')':
                newOrigins.append(prot)
    return list(set(newOrigins))


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
        # combined eg: ['ABC', 'BCA', 'ACD', 'DCA']
        # combinedRef eg: [[0,1,2], [1,0,2], [0,2,3], [3,2,0]]
        # pass splits through combined overlap peptide and then delete all duplicates
        combined, combinedRef, linSet = combineOverlapPeptide(splits, splitRef, mined, maxed, overlapFlag, maxDistance)

    elif spliceType == LINEAR:
        # Explicit change for high visibility regarding what's happening
        combined, combinedRef = splits, splitRef
        linSet = set()

    return combined, combinedRef, linSet

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
                    newValue = [newMass, currentRef]
                    # if trans is running, the original protein tuple must be updated with the modified peptide
                    try:
                        newValue.append(combineModlessDict[string][2])
                    except IndexError:
                        print('')
                    modDict[temp] = newValue
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


def combineOverlapPeptide(splits, splitRef, mined, maxed, overlapFlag, maxDistance):

    """
    Input: splits: list of splits, splitRef: list of the character indexes for splits, mined/maxed: min and max
    size requirements, overlapFlag: boolean value true if overlapping combinations are undesired.
    Output: all combinations of possible splits which meets criteria
    """
    # initialise linSet
    linSet = set()
    # initialise combinations array to hold the possible combinations from the input splits
    massDict = {}
    combModless = []
    combModlessRef = []
    # iterate through all of the splits and build up combinations which meet min/max/overlap criteria
    for i in range(0, len(splits)):

        # toAdd variables hold temporary combinations for insertion in final matrix if it meets criteria
        toAddForward = ""

        toAddReverse = ""

        for j in range(i, len(splits)):
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
                        #check if linear and add to linearSet if so
                        linSet = addLinPeptides(toAddForward, addForwardRef, linSet, False)
                        massDict[toAddForward] = addForwardRef
                        massDict[toAddReverse] = addReverseRef

                else:
                    linSet = addLinPeptides(toAddForward, addForwardRef, linSet, False)
                    massDict[toAddForward] = addForwardRef
                    massDict[toAddReverse] = addReverseRef
            elif not maxDistCheck(splitRef[i], splitRef[j], maxDistance):
                break

            toAddForward = ""
            toAddReverse = ""

    for peptide, ref in massDict.items():
        if peptide in linSet:
            continue
        else:
            combModless.append(peptide)
            combModlessRef.append(ref)

    return combModless, combModlessRef, linSet

def addLinPeptides(peptide, refs, linCisSet, transOrigins):
    prevRef = refs[0]
    for i in range(1,len(refs)):
        if transOrigins != False:
            prot1, index1 = findInitProt(refs[0]-1, transOrigins)
            prot2, index2 = findInitProt(refs[-1]-1, transOrigins)
            if prot1 == prot2:
                if len(set(refs)) == len(refs):
                    linCisSet.add(peptide)
                return linCisSet
            else:
                return linCisSet
        elif refs[i] == prevRef + 1:
            prevRef = refs[i]
        else:
            return linCisSet
    linCisSet.add(peptide)
    return linCisSet

def chargeIonMass(massDict, chargeFlags):

    """
    chargeFlags: [True, False, True, False, True]
    """

    for key, value in massDict.items():
        chargeAssoc = {}
        for z in range(0, len(chargeFlags)):

            if chargeFlags[z]:
                chargeMass = massCharge(value[0], z+1)  # +1 for actual value
                if mgfData is None:
                    chargeAssoc[z+1] = chargeMass

                # Make sure the chargemass is less than the maximum possible charge mass in the mgf
                elif chargeMass <= mgfData.chargeMaxDict[z+1]:
                    chargeAssoc[z+1] = chargeMass
        if chargeAssoc:
            # Add it to the 2 as the rest of code acceses it at index 2
            value.insert(2, chargeAssoc)
        else:
            try:
                # Delete the key if there are no charges that pass the max mass test
                del massDict[key]
            except KeyError:
                pass


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
    protList = []
    ind = 0
    for key, value in seqDict.items():
        dictlist.append(value)
        protIndexList.append([ind,ind + len(value) - 1])
        protList.append(key)
        ind += len(value)

    # print(protIndexList)

    finalPeptide = ''.join(dictlist)
    return finalPeptide, protIndexList, protList


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


def combMass(combine, combineRef, origProtTups = None):
    massDict = {}
    try:
        maxMass = mgfData.maxMass
        # print('in maxmass')
    except:
        maxMass = 1000000
    for i in range(0, len(combine)):
        totalMass = 0
        for j in range(0, len(combine[i])):
            totalMass += monoAminoMass[combine[i][j]]
        totalMass += H20_MASS
        if totalMass > maxMass:
            print(combine[i])
            continue
        if origProtTups == None:
            massRefPair = [totalMass, combineRef[i]]
            massDict[combine[i]] = massRefPair
        # when trans is being run, peptide which have already been added to massDict need their original peptide location
        # information updated so it is not lost in the running process.
        else:
            if combine[i] in massDict.keys():
                massDict[combine[i]][2].append(origProtTups[i][0])
                massDict[combine[i]][2].append(origProtTups[i][1])
            else:
                massRefPair = [totalMass, combineRef[i], origProtTups[i]]
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


def processLockInit(lockVar, toWriteQueue, pepCompleted, mgfObj, childTable, linSetQueue):

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
    genMassDict.linSetQueue = linSetQueue

def processLockTrans(lockVar, toWriteQueue, pepCompleted, allSplits, allSplitRef, mgfObj, childTable,linCisQueue):

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
    transProcess.linCisQueue = linCisQueue


